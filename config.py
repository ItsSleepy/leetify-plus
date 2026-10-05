import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    DISCORD_TOKEN   = os.getenv('DISCORD_TOKEN')
    LEETIFY_API_KEY = os.getenv('LEETIFY_API_KEY', 'f9d02f9d-684f-47c5-aed8-d7803a4d37dc')

    @classmethod
    def validate(cls):
        if not cls.DISCORD_TOKEN:
            raise ValueError('DISCORD_TOKEN is required in .env')
        return True

try:
    Config.validate()
except ValueError as e:
    print(f'Config Error: {e}')

