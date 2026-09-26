from adapters.chat.base import ChatAdapter
from adapters.chat.slack import SlackAdapter
from settings import get_setting


def get_slack_adapter() -> ChatAdapter:
    # Read per call so a token rotated in the Admin UI applies without a restart.
    return SlackAdapter(get_setting("SLACK_BOT_TOKEN", ""))
