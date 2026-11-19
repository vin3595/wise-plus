from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="WISE_", extra="ignore")

    # App database user. Must NOT be a superuser or table owner, otherwise
    # Postgres row-level security is bypassed.
    database_url: str = "postgresql+psycopg://wise_app:wise_app@localhost:5432/wise"
    # Migration user (table owner). Used only by Alembic.
    migrate_database_url: str = "postgresql+psycopg://wise_owner:wise_owner@localhost:5432/wise"

    jwt_secret: str = "change-me-in-production"
    owner_token_hours: int = 24 * 30
    staff_token_hours: int = 16

    otp_provider: str = "console"  # console (dev: code shown in the browser) | box (code in the log) | msg91 | fast2sms
    signup_mode: str = "open"      # open | first (Wise Box) | closed
    otp_ttl_minutes: int = 10
    otp_max_attempts: int = 5
    msg91_auth_key: str = ""
    msg91_template_id: str = ""
    fast2sms_api_key: str = ""
    # WhatsApp Cloud API (Meta), wise-plus. Empty token = WhatsApp off.
    wa_token: str = ""              # permanent (system user) access token
    wa_phone_number_id: str = ""    # the business number's ID in Meta
    wa_business_id: str = ""        # WhatsApp Business Account ID (for templates)
    wa_app_secret: str = ""         # checks that webhook calls really come from Meta
    wa_verify_token: str = ""       # what Meta sends back once when the webhook is set up
    wa_graph_version: str = "v23.0"
    wa_nightly_template: str = ""
    wa_bill_template: str = ""       # customers' bill with feedback buttons (4 body values, 3 quick replies)
    wa_promo_template: str = "promo_message"          # wise-plus: marketing broadcast ('Stop offers' button)
    wa_promo_offer_template: str = "promo_offer"      # the same with a 'Get my offer' button first
    wa_display_number: str = ""          # wise-plus: the WhatsApp number customers message (offer posters)
    wa_update_template: str = "wise_update"   # utility template: a reply sent after the 24-hour window closed
    wa_app_id: str = ""                       # Meta app id (Connect WhatsApp for restaurants' own numbers)
    wa_es_config_id: str = ""                 # Embedded Signup configuration id (from the Meta app, once a Tech Provider)
    wa_promo_daily_cap: int = 1000            # broadcasts: most a restaurant may send in a day from the shared number
    legal_name: str = ""                     # who runs Ease My Bill, as on the PAN (policy pages, Razorpay/Paytm review)
    contact_email: str = ""
    contact_address: str = ""
    meta_pixel_id: str = ""                   # Meta (Facebook/Instagram) pixel on the landing page, for ads
    wa_growth_template: str = "emb_followup"  # marketing template: follow-ups to people who asked about Ease My Bill
    wa_shared_marketing: bool = False         # broadcasts on the shared number for everyone (off: only own numbers + house)
    wa_otp_template: str = "login_code"   # authentication template for sign-up / sign-in codes (WISE_OTP_PROVIDER=whatsapp)   # approved template for the nightly summary outside the 24-hour window
    nightly_at: str = "23:30"       # India time; "" turns the nightly WhatsApp summary off

    pin_max_failures: int = 5
    pin_lock_minutes: int = 10

    # Restaurants get <login_id>.<base_domain>, e.g. shakewallah.wise.in
    base_domain: str = "localhost"
    upload_dir: str = "uploads"
    public_base_url: str = "http://localhost:8000"
    cors_origins: str = "http://localhost:5173"
    trial_days: int = 14
    # wise-plus billing (Razorpay, your own account: subscriptions for wise-plus itself)
    billing_rz_key_id: str = ""
    billing_rz_key_secret: str = ""
    billing_rz_webhook_secret: str = ""
    platform_admin_phones: str = ""  # wise-plus: who runs Ease My Bill (10-digit, comma separated): signs in at /admin, separate from any restaurant
    house_tenants: str = ""          # login ids of your own businesses: never billed, Daily cash sheet
    # Address customers' phones use for QR menus and the order-ready screen (the Wise Box's public
    # Cloudflare address). Empty = the address the staff opened Wise from.
    customer_url: str = ""
    sms_worker: bool = True        # send the SMS outbox from this process
    live_updates: bool = True      # push changes to screens (Postgres LISTEN/NOTIFY + Server-Sent Events)


@lru_cache
def get_settings() -> Settings:
    return Settings()
