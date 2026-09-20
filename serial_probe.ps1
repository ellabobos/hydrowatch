param([string]$Port = "COM6", [int]$Baud = 115200, [int]$Seconds = 8)
$p = New-Object System.IO.Ports.SerialPort($Port, $Baud, [System.IO.Ports.Parity]::None, 8, [System.IO.Ports.StopBits]::One)
$p.ReadTimeout = 1200
$p.NewLine = "`n"
$p.DtrEnable = $true
$p.RtsEnable = $true
try {
    $p.Open()
    $p.DiscardInBuffer()
    $p.Write("`n")
    Write-Output "OPEN $Port @$Baud OK - reading for $Seconds s..."
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $line = $p.ReadLine()
            Write-Output "<< $line"
        } catch [TimeoutException] {}
    }
    $p.Close()
    Write-Output "CLOSED"
} catch {
    Write-Output "ERR $($_.Exception.Message)"
}
