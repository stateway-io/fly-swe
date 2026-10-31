import os

# `import litellm` calls `dotenv.load_dotenv()` unless LITELLM_MODE is set to
# something other than "DEV", and find_dotenv walks up from litellm's install
# dir. With the venv inside the repo that finds the developer's real `.env`,
# so its PROVIDERS__* secrets land in os.environ and leak into every Settings
# built by a later test. Must run before any test module imports litellm.
os.environ.setdefault("LITELLM_MODE", "PRODUCTION")
