#include <Arduino_Modulino.h>
#include <DHT.h>

ModulinoDistance distance;
ModulinoMovement movement;
DHT dht(2, DHT11);

// Moisture sensor on analog pin A3
const int MOISTURE_PIN = A3;

// DHT11 needs >= 1 s between reads; sample every 4th loop (2 s)
unsigned int loopCount = 0;
float humidity = NAN;
float tempC = NAN;

void setup() {
  Serial.begin(115200);
  // The UNO Q runs Zephyr; brief startup pause so the serial monitor
  // attaches before the first readings are printed.
  delay(2500);

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
  Serial.println("---- hydrowatch start ----");
}

void loop() {
  int moistureRaw = analogRead(MOISTURE_PIN);

  // ModulinoDistance returns millimetres; -1 means no new sample this cycle
  int mm = distance.available() ? distance.get() : -1;

  // Movement (IMU): acceleration in milli-g, ~1000 mg = 1 g
  movement.update();
  long ax = (long)(movement.getX() * 1000.0);
  long ay = (long)(movement.getY() * 1000.0);
  long az = (long)(movement.getZ() * 1000.0);

  if (loopCount % 4 == 0) {
    float h = dht.readHumidity();
    float t = dht.readTemperature();
    if (!isnan(h)) humidity = h;
    if (!isnan(t)) tempC = t;
  }
  loopCount++;

  Serial.print("moisture=");
  Serial.print(moistureRaw);
  Serial.print(" | distance_mm=");
  Serial.print(mm);
  Serial.print(" | humidity=");
  Serial.print(isnan(humidity) ? -1 : (int)round(humidity));
  Serial.print(" | temp_c=");
  Serial.print(isnan(tempC) ? -999 : (int)round(tempC));
  Serial.print(" | ax=");
  Serial.print(ax);
  Serial.print(" | ay=");
  Serial.print(ay);
  Serial.print(" | az=");
  Serial.println(az);

  delay(500);
}
