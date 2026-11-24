import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Security limits are per client address; every test comes from the same test address, so they're off for the
# suite and switched on in tests/test_ratelimit.py. Production keeps the defaults (on).
os.environ.setdefault("WISE_RATE_LIMITS", "0")
os.environ.setdefault("WISE_OTP_HOURLY_CAP", "1000000")
