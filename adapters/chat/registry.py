from adapters.chat.base import ChatAdapter
from adapters.chat.slack import SlackAdapter
from config import SLACK_BOT_TOKEN


def get_slack_adapter() -> ChatAdapter:
    return SlackAdapter(SLACK_BOT_TOKEN)
