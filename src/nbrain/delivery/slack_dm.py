"""Post the brief to the user's own Slack DM (or a channel they chose)."""

from __future__ import annotations

import asyncio
import re

from nbrain.config.loader import get_secret
from nbrain.config.schema import Config
from nbrain.delivery.base import DeliveryResult, RenderedBrief


def md_to_mrkdwn(md: str) -> str:
    out = md
    out = re.sub(r"^#{1,6}\s*(.+)$", r"*\1*", out, flags=re.M)
    out = re.sub(r"\*\*(.+?)\*\*", r"*\1*", out)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", out)
    out = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m.group(2) or m.group(1), out)
    out = re.sub(r"^\|.*\|$\n?", "", out, flags=re.M)  # drop markdown tables
    out = re.sub(r"^>\s*\[!\w+\]\s*", "> ", out, flags=re.M)
    out = re.sub(r"^---$", "", out, flags=re.M)
    return out.strip()


class SlackDMDeliverer:
    name = "slack_dm"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _post_sync(self, brief: RenderedBrief) -> str:
        from slack_sdk import WebClient

        token = get_secret(self.cfg.sources.slack.token_env)
        if not token:
            raise RuntimeError(f"no Slack token in {self.cfg.sources.slack.token_env}")
        client = WebClient(token=token)
        channel = self.cfg.delivery.slack_dm.channel
        if not channel:
            me = self.cfg.sources.slack.user_id or client.auth_test()["user_id"]
            channel = client.conversations_open(users=[me])["channel"]["id"]
        text = md_to_mrkdwn(brief.markdown)
        chunks = [text[i : i + 3500] for i in range(0, len(text), 3500)] or [text]
        ts = None
        for chunk in chunks:
            res = client.chat_postMessage(channel=channel, text=chunk, thread_ts=ts, unfurl_links=False)
            ts = ts or res["ts"]
        return f"posted to {channel} ({len(chunks)} message(s))"

    async def deliver(self, brief: RenderedBrief) -> DeliveryResult:
        detail = await asyncio.to_thread(self._post_sync, brief)
        return DeliveryResult(self.name, True, detail)
