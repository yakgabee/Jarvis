import os

# Test settings — set before jarvis.config is imported.
os.environ.update(
    {
        "ANTHROPIC_API_KEY": "test-key",
        "TWILIO_ACCOUNT_SID": "ACtest",
        "TWILIO_AUTH_TOKEN": "test-auth-token",
        "TWILIO_PHONE_NUMBER": "+15550000000",
        "ALLOWED_NUMBERS": "+15551112222",
        "OWNER_NAME": "Tony",
        "TIMEZONE": "America/New_York",
        "HOME_CITY": "New York",
        "PUBLIC_URL": "https://jarvis.example.com",
        "DATABASE_PATH": ":memory:",
        "ENABLE_WEB_SEARCH": "true",
    }
)
