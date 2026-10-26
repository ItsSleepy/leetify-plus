import os
from dotenv import load_dotenv

load_dotenv()


def _key_file():
    """Leetify API key from leetify.key next to this file (kept out of git), if not set in the environment."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'leetify.key')) as f:
            return f.read().strip() or None
    except OSError:
        return None

class Config:
    DISCORD_TOKEN   = os.getenv('DISCORD_TOKEN')
    LEETIFY_API_KEY = os.getenv('LEETIFY_API_KEY') or _key_file()

    @classmethod
    def validate(cls):
        if not cls.DISCORD_TOKEN:
            raise ValueError('DISCORD_TOKEN is required in .env')
        return True

try:
    Config.validate()
except ValueError as e:
    print(f'Config Error: {e}')

