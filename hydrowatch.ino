#include <Arduino_Modulino.h>
#include <DHT.h>
#include "firmware/model.h"

ModulinoDistance distance;
ModulinoMovement movement;
DHT dht(2, DHT11);

// Moisture sensor on analog pin A3
const int MOISTURE_PIN = A3;

// ---- Buzzer alarm on D7 (MCU-local: sounds even if Linux/network die) ----
// BUZZER_ACTIVE=true  -> active buzzer or buzzer module (makes its own tone)
// BUZZER_ACTIVE=false -> passive buzzer (we drive a square wave instead)
const int BUZZER_PIN = 7;
const bool BUZZER_ACTIVE = true;
const int BUZZER_ON_LEVEL = HIGH;   // flip to LOW for active-low trigger modules

// Alarm policy: probability must be high, SUSTAINED, and corroborated.
const float P_ALARM_ON    = 0.70f;  // arm threshold
const float P_ALARM_OFF   = 0.55f;  // release threshold (hysteresis, no chatter)
const int   ARM_LOOPS     = 8;      // 8 x 0.5 s = 4 s sustained to latch
const float RISE_ARM_MM_S = 0.1f;   // water must be moving (6 mm/min): stops a
                                    // frozen/bench-mounted ToF from howling

// ---- On-MCU flood classifier v2 (quantized forest, see model.h) ----
// 9 features, mirroring sensor_sim.py v2:
//   f_moisture_wet      8 s mean of (1023 - moisture_raw)
//   f_moisture_slope    wetness change over 8 s (counts/s)
//   f_dist_mm           2.5 s mean of ToF distance
//   f_water_rise_mm_s   water rise over 8 s (mm/s, >= 0)
//   f_temp_c, f_humidity DHT11 (nominal until first valid read)
//   f_imu_peak_mg       8 s PEAK of | |a|-1g | (transient survives a peak;
//                       a mean dilutes a debris strike into the noise floor)
//   f_sat_rain          NASA-observed rain, 0..1 (confirmation channel)
//   f_fc_rain           forecast rain, 0..1 (lead-time channel)
// Weather arrives from the Linux side as "SKY sat=X fc=Y" lines; until the
// first one arrives the model assumes dry sky (0/0) — the safe default.
const int WET_WIN = 16;   // 16 loops x 0.5 s = 8 s
const int DIST_WIN = 5;   // 5 loops x 0.5 s = 2.5 s
const float LOOP_S = 0.5f;
const float WIN_S = 8.0f;

float wetHist[WET_WIN];
float distHist[DIST_WIN];
float imuDevHist[WET_WIN];
int histIdx = 0;
bool wetFilled = false, distFilled = false;

float wetMean = 0.0f, distMean = 877.0f;
float wetMean8sAgo = 0.0f, distMean8sAgo = 877.0f;
bool slopeValid = false;
unsigned long loopCount = 0;

float lastGoodMm = 877.0f;
float humidity = NAN;
float tempC = NAN;

// Bridged weather features (Linux -> MCU via SKY lines)
float satRain = 0.0f;
float fcRain = 0.0f;
bool skyReceived = false;

// Buzzer alarm state
bool alarmOn = false;
int aboveCnt = 0;
int patternSlot = 0;
unsigned long testUntil = 0;   // >0 while a BUZZ TEST self-test is sounding

// Serial input line buffer (non-blocking parse)
char inBuf[64];
int inLen = 0;

// Nominal DHT values used until the sensor produces a valid reading
const float NOMINAL_TEMP_C = 22.0f;
const float NOMINAL_HUM = 45.0f;

void buzzOn() {
  if (BUZZER_ACTIVE) digitalWrite(BUZZER_PIN, BUZZER_ON_LEVEL);
  else tone(BUZZER_PIN, 2700);
}

void buzzOff() {
  if (BUZZER_ACTIVE) digitalWrite(BUZZER_PIN, !BUZZER_ON_LEVEL);
  else noTone(BUZZER_PIN);
}

void setup() {
  Serial.begin(115200);
  delay(2500);   // Zephyr boot: let the serial monitor attach

  pinMode(BUZZER_PIN, OUTPUT);
  buzzOff();     // ensure silent at boot

  Modulino.begin();

  if (distance.begin()) {
    Serial.println("MODULINO_DISTANCE OK");
  } else {
    Serial.println("MODULINO_DISTANCE NOT FOUND - check QWIIC cable");
  }

  if (movement.begin()) {
    Serial.println("MODULINO_MOVEMENT OK");
  } else {
    Serial.println("MODULINO_MOVEMENT NOT FOUND - check QWIIC chain");
  }

  dht.begin();
  Serial.println("DHT11 on D2 READY");
  Serial.println("A3 MOISTURE READY");
  Serial.println("HW_MODEL: quantized forest v2 loaded (9 features)");
  Serial.println("---- hydrowatch start ----");
}

// Parse "SKY sat=0.42 fc=0.18" lines from the Linux side
void handleSerialInput() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (inLen > 0) {
        inBuf[inLen] = '\0';
        float s, f;
        if (sscanf(inBuf, "SKY sat=%f fc=%f", &s, &f) == 2) {
          satRain = s;
          fcRain = f;
          if (!skyReceived) {
            skyReceived = true;
            Serial.println("SKY LINK UP");
          }
        } else if (strcmp(inBuf, "BUZZ TEST") == 0) {
          testUntil = millis() + 3000;   // 3 s sounder self-test
          Serial.println("BUZZ TEST ACK");
        }
        inLen = 0;
      }
    } else if (inLen < 63) {
      inBuf[inLen++] = c;
    } else {
      inLen = 0;  // overflow guard: drop the line
    }
  }
}

void loop() {
  handleSerialInput();

  int moistureRaw = analogRead(MOISTURE_PIN);

  // ModulinoDistance returns millimetres; -1 means no new sample this cycle
  int mm = distance.available() ? distance.get() : -1;
  if (mm > 0) lastGoodMm = (float)mm;

  // Movement (IMU): acceleration in milli-g, ~1000 mg = 1 g
  movement.update();
  float ax = movement.getX() * 1000.0f;
  float ay = movement.getY() * 1000.0f;
  float az = movement.getZ() * 1000.0f;
  // All-zero vector = module dropped off the I2C bus (observed failure mode):
  // report deviation 0 so the model recognizes the fault state it was
  // trained on, instead of a fake 1000 mg "impact".
  bool imuDead = (ax == 0.0f && ay == 0.0f && az == 0.0f);
  float mag = sqrtf(ax * ax + ay * ay + az * az);
  float dev = imuDead ? 0.0f : fabsf(mag - 1000.0f);

  if (loopCount % 4 == 0) {
    float h = dht.readHumidity();
    float t = dht.readTemperature();
    if (!isnan(h)) humidity = h;
    if (!isnan(t)) tempC = t;
  }
  loopCount++;

  // ---- Maintain circular windows ----
  wetHist[histIdx % WET_WIN] = 1023.0f - (float)moistureRaw;
  distHist[histIdx % DIST_WIN] = lastGoodMm;
  imuDevHist[histIdx % WET_WIN] = dev;
  histIdx++;
  if (histIdx >= WET_WIN) wetFilled = true;
  if (histIdx >= DIST_WIN) distFilled = true;

  if (wetFilled) {
    // Snapshot BEFORE refreshing so slopes span two distinct 8 s windows
    if (loopCount % 16 == 0) {
      wetMean8sAgo = wetMean;
      distMean8sAgo = distMean;
      slopeValid = true;
    }
    float s = 0;
    for (int i = 0; i < WET_WIN; ++i) s += wetHist[i];
    wetMean = s / WET_WIN;
    if (distFilled) {
      float s2 = 0;
      for (int i = 0; i < DIST_WIN; ++i) s2 += distHist[i];
      distMean = s2 / DIST_WIN;
    }
  }

  // ---- Feature vector ----
  float f_moisture_wet = wetFilled ? wetMean : (1023.0f - (float)moistureRaw);
  float f_moisture_slope = slopeValid ? (wetMean - wetMean8sAgo) / WIN_S : 0.0f;
  float f_dist_mm = distFilled ? distMean : lastGoodMm;
  float f_water_rise = slopeValid ? (distMean8sAgo - distMean) / WIN_S : 0.0f;
  if (f_water_rise < 0) f_water_rise = 0;
  if (f_moisture_slope < -100) f_moisture_slope = -100;
  if (f_moisture_slope > 100) f_moisture_slope = 100;

  float f_imu_peak = 0;
  if (wetFilled) {
    for (int i = 0; i < WET_WIN; ++i) if (imuDevHist[i] > f_imu_peak) f_imu_peak = imuDevHist[i];
  }

  float feats[HW_FEATURE_COUNT] = {
    f_moisture_wet,
    f_moisture_slope,
    f_dist_mm,
    f_water_rise,
    isnan(tempC) ? NOMINAL_TEMP_C : tempC,
    isnan(humidity) ? NOMINAL_HUM : humidity,
    f_imu_peak,
    satRain,
    fcRain,
  };

  // ---- On-MCU inference (gated until feature windows are valid) ----
  int mcls = -1;
  float pFlood = -1.0f;
  if (wetFilled && distFilled && slopeValid) {
    int votes[HW_CLASS_COUNT];
    mcls = HW_MODEL_predict(feats, votes);
    int floodVotes = votes[2] + votes[3];
    int totalVotes = 0;
    for (int c = 0; c < HW_CLASS_COUNT; ++c) totalVotes += votes[c];
    pFlood = totalVotes > 0 ? (float)floodVotes / (float)totalVotes : 0.0f;
  }

  // ---- Buzzer alarm state machine (D7) ----
  if (pFlood < 0) {
    aboveCnt = 0;   // warming up: silent, no arm progress
  } else if (!alarmOn) {
    bool corroborated = (f_water_rise >= RISE_ARM_MM_S) || (mcls == 3);
    if (pFlood >= P_ALARM_ON && corroborated) aboveCnt++;
    else aboveCnt = 0;
    if (aboveCnt >= ARM_LOOPS) { alarmOn = true; patternSlot = 0; }
  } else if (pFlood < P_ALARM_OFF) {
    alarmOn = false;
  }

  bool testActive = millis() < testUntil;
  if (testActive) {
    patternSlot++;
    buzzOn();                                        // self-test: continuous 3 s
  } else if (alarmOn && pFlood >= 0) {
    patternSlot++;
    bool on;
    if (mcls == 3) on = true;                        // debris impact: continuous siren
    else if (mcls == 2) on = (patternSlot % 3) != 0; // flood rise: 1 s on / 0.5 s off
    else on = (patternSlot % 4) == 1;                // other: 0.5 s on / 1.5 s off
    if (on) buzzOn(); else buzzOff();
  } else {
    buzzOff();
  }

  Serial.print("moisture=");
  Serial.print(moistureRaw);
  Serial.print(" | distance_mm=");
  Serial.print(mm);
  Serial.print(" | humidity=");
  Serial.print(isnan(humidity) ? -1 : (int)round(humidity));
  Serial.print(" | temp_c=");
  Serial.print(isnan(tempC) ? -999 : (int)round(tempC));
  Serial.print(" | ax=");
  Serial.print((long)ax);
  Serial.print(" | ay=");
  Serial.print((long)ay);
  Serial.print(" | az=");
  Serial.print((long)az);
  Serial.print(" | p_flood=");
  if (pFlood < 0) Serial.print("-1"); else Serial.print(pFlood, 3);
  Serial.print(" | mcls=");
  Serial.print(mcls);
  Serial.print(" | imu_peak=");
  Serial.print((int)f_imu_peak);
  Serial.print(" | buzz=");
  Serial.print((alarmOn || millis() < testUntil) ? "on" : "off");
  Serial.print(" | sky=");
  if (skyReceived) {
    Serial.print(satRain, 2);
    Serial.print("/");
    Serial.println(fcRain, 2);
  } else {
    Serial.println("none");
  }

  delay(500);
}
