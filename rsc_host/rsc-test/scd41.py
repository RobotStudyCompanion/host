"""
tests/scd41.py — SCD41 CO2 / temperature / relative humidity over I2C.

Takes a single-shot measurement and prints the result.
Requires: pip install adafruit-circuitpython-scd4x
"""

from config import I2C as I2CCfg


# ── Check ─────────────────────────────────────────────────────────────────────

def check():
    """Scan I2C bus for the SCD41 at the expected address."""
    try:
        import smbus2
        bus = smbus2.SMBus(I2CCfg.BUS)
        try:
            bus.read_byte(I2CCfg.SCD41_ADDR)
            bus.close()
            return True, f"SCD41 found at I2C bus {I2CCfg.BUS} addr 0x{I2CCfg.SCD41_ADDR:02X}"
        except OSError:
            bus.close()
            return False, f"No device at I2C bus {I2CCfg.BUS} addr 0x{I2CCfg.SCD41_ADDR:02X} — is SCD41 connected?"
    except ImportError:
        return False, "smbus2 not installed — pip install smbus2"
    except Exception as e:
        return False, str(e)


# ── Run ───────────────────────────────────────────────────────────────────────

def run():
    """Take a single-shot measurement and print CO2, temperature, and RH."""
    try:
        import board
        import adafruit_scd4x
    except ImportError:
        print("missing dependency — pip install adafruit-circuitpython-scd4x")
        return

    import busio
    i2c = busio.I2C(board.SCL, board.SDA)
    scd = adafruit_scd4x.SCD4X(i2c)

    print("starting single-shot measurement (5 s)...")
    scd.start_periodic_measurement()

    import time
    timeout = 10
    start   = time.time()
    while not scd.data_ready:
        if time.time() - start > timeout:
            print("timeout — no data from SCD41 after 10 s")
            scd.stop_periodic_measurement()
            return
        time.sleep(0.5)

    print(f"  CO2          : {scd.CO2} ppm")
    print(f"  Temperature  : {scd.temperature:.1f} °C")
    print(f"  Humidity     : {scd.relative_humidity:.1f} %RH")

    scd.stop_periodic_measurement()
