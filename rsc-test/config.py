"""
config.py — RSC hardware configuration.
Single source of truth for all pin assignments, tuning values, and subsystem settings.
Edit this file; everything else imports from here.
"""

# ── GPIO ──────────────────────────────────────────────────────────────────────
class GPIO:
    BUTTON          = 23    # arcade button — idles low, high on press
    LED_PWM         = 24    # arcade button LED via Q1
    RING            = 12    # SKC6812 RGBW NeoPixel ring via J6
    SERVO_LEFT      = 13    # M2_PWM via J7
    SERVO_RIGHT     = 26    # M3_PWM via J8
    RESPEAKER_BTN   = 17    # ReSpeaker HAT user button


# ── Servos ────────────────────────────────────────────────────────────────────
class Servo:
    STOP            = 1500  # µs — neutral / coast

    LEFT_FWD        = 1600  # µs
    LEFT_REV        = 1400  # µs

    # Right motor is mechanically mirrored — trim corrects for drift.
    # Increase R_TRIM to speed up right, decrease to slow it down.
    R_TRIM          = 105   # µs

    RIGHT_FWD       = 1400 - R_TRIM    # = 1295 µs
    RIGHT_REV       = 1600 + R_TRIM    # = 1705 µs

    HOLD_TIME_S     = 1.0   # seconds before gpiozero fires when_held


# ── NeoPixel ring ─────────────────────────────────────────────────────────────
class Ring:
    COUNT           = 16
    BRIGHTNESS      = 0.3
    PIXEL_ORDER     = "GRBW"   # SKC6812 RGBW — passed to neopixel as neopixel.GRBW

    COLOUR_PRESS    = (0, 80, 255, 0)   # RGBW — blue, white off
    COLOUR_OFF      = (0,  0,   0, 0)
    SWEEP_DELAY_S   = 0.06


# ── Arcade button ─────────────────────────────────────────────────────────────
class Button:
    BOUNCE_TIME_S   = 0.05


# ── ReSpeaker 2-Mic HAT ───────────────────────────────────────────────────────
class ReSpeaker:
    # APA102 RGB LEDs on SPI — count and default colour
    LED_COUNT       = 3
    LED_BRIGHTNESS  = 0.2   # 0.0–1.0
    LED_COLOUR_IDLE = (0,   0,  30)     # dim blue
    LED_COLOUR_REC  = (180, 0,   0)     # red while recording


# ── Audio ─────────────────────────────────────────────────────────────────────
class Audio:
    ALSA_CAPTURE    = "hw:0,0"
    ALSA_PLAYBACK   = "plughw:0,0"
    SAMPLE_RATE     = 16000
    CHANNELS        = 2
    BIT_DEPTH       = "S16_LE"
    RECORD_SECS     = 3
    TMP_WAV         = "/tmp/rsc_test_audio.wav"


# ── I2C ───────────────────────────────────────────────────────────────────────
class I2C:
    BUS             = 1
    SCD41_ADDR      = 0x62     # CO2 / temperature / relative humidity


# ── UART → CYD ────────────────────────────────────────────────────────────────
class UART:
    PORT            = "/dev/ttyAMA0"
    BAUD            = 115200
    TIMEOUT_S       = 2.0
    # Commands the test suite uses to probe the CYD
    PROBE_CMDS      = ["version", "status", "ldr"]


# ── Ollama ────────────────────────────────────────────────────────────────────
class Ollama:
    ENDPOINT        = "http://localhost:11434"
    HEALTH_PATH     = "/api/tags"
