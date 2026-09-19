"""Gmail draft-to-self (Rule 2) or send-to-self. Both address only the user."""

from __future__ import annotations

import asyncio
import base64
from email.message import EmailMessage
from typing import Literal

from nbrain.brief.build import to_plain_text
from nbrain.config.schema import Config
from nbrain.delivery.base import DeliveryResult, RenderedBrief


class GmailDeliverer:
    def __init__(self, cfg: Config, mode: Literal["draft", "send"]):
        self.cfg = cfg
        self.mode = mode
        self.name = f"gmail_{mode}"

    def _message(self, brief: RenderedBrief) -> dict[str, str]:
        msg = EmailMessage()
        me = self.cfg.user.email
        if not me:
            raise RuntimeError("user.email is empty; refusing to address a brief to nobody")
        msg["To"] = me
        msg["From"] = me
        msg["Subject"] = brief.subject
        msg.set_content(to_plain_text(brief.markdown))
        if brief.html:
            msg.add_alternative(brief.html, subtype="html")
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return {"raw": raw}

    def _send_sync(self, brief: RenderedBrief) -> str:
        from nbrain.sources.google.auth import GoogleAuth

        auth = GoogleAuth(self.cfg)
        svc = auth.service("gmail", "v1")
        body = self._message(brief)
        if self.mode == "draft":
            res = svc.users().drafts().create(userId="me", body={"message": body}).execute()
            return f"draft {res.get('id')} created, addressed to {self.cfg.user.email} only"
        res = svc.users().messages().send(userId="me", body=body).execute()
        return f"message {res.get('id')} sent to {self.cfg.user.email}"

    async def deliver(self, brief: RenderedBrief) -> DeliveryResult:
        detail = await asyncio.to_thread(self._send_sync, brief)
        return DeliveryResult(self.name, True, detail)
