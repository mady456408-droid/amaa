"""ChatGPT Client - Core API Client.

This module contains only the ChatGPT API client logic, without any bot-specific code.
It can be used by both the Telegram bot and the rewrite functionality with separate config files.
"""

import requests
import json
import uuid
import os
import time


class ChatGPT:
    """ChatGPT API client for Android endpoint.
    
    This client handles authentication, session management, and API communication
    with the ChatGPT Android endpoint. It can be instantiated with custom config
    and cookie file paths to allow multiple independent instances.
    """
    
    def __init__(self, config_file="chatgpt_config.json", cookies_file="chatgpt_cookies.json"):
        """Initialize ChatGPT client.
        
        Args:
            config_file: Path to config file for storing device_id, tokens, etc.
            cookies_file: Path to cookies file for storing session cookies.
        """
        self.config_file = config_file
        self.cookies_file = cookies_file
        
        self.session = requests.Session()
        self.payload_config = self._get_default_payload_config()
        self.device_id = None
        self.conduit_token = None
        self.chat_req_token = None
        self.play_integrity_token = None
        self.convo_session_id = None
        self.turn_trace_id = None

        self.base_url = "https://android.chat.openai.com"
        self.prepare_path = "/backend-anon/f/conversation/prepare"
        self.sentinel_path = "/backend-anon/sentinel/chat-requirements"
        self.conversation_path = "/backend-anon/f/conversation"
        self.user_agent = "ChatGPT/1.2026.195 (Android 15; RMX3834; build 2619512)"
        self.device_tier = "lower_mid"
        self.account_id = "default"
        self.residency_region = "no_constraint"
        self.accept_language = "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7"
        self.timezone = "Africa/Cairo"
        self.timezone_offset = -180

        self.load_state()
        if not self.device_id:
            self.device_id = str(uuid.uuid4())
            self.save_state()
        self.init_session()

    def _get_default_payload_config(self):
        """Get default payload configuration."""
        return {
            "model": "auto",
            "history_and_training_disabled": False,
            "enable_message_followups": True,
            "force_use_sse": True,
            "force_use_search": None,
            "force_paragen": False,
            "supports_buffering": False,
            "timezone": "Africa/Cairo",
            "timezone_offset_min": -180,
            "system_hints": [],
            "is_onboarding_conversation": False,
            "no_auth_ad_preferences": {"personalization_enabled": False, "history_enabled": True},
            "client_prepare_dispatch": "debounced",
            "client_prepare_source": "composer_editor_state",
            "client_prepare_state": "success"
        }

    def load_state(self):
        """Load state from config and cookies files."""
        if os.path.exists(self.config_file):
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.device_id = data.get("device_id")
            self.play_integrity_token = data.get("play_integrity_token", "")
            self.conduit_token = data.get("conduit_token", "")
            if "payload_config" in data:
                for k, v in data["payload_config"].items():
                    if k in self.payload_config:
                        self.payload_config[k] = v
            for attr in ["base_url","prepare_path","sentinel_path","conversation_path",
                          "user_agent","device_tier","account_id","residency_region",
                          "accept_language","timezone","timezone_offset"]:
                if attr in data:
                    setattr(self, attr, data[attr])
        if os.path.exists(self.cookies_file):
            with open(self.cookies_file, "r") as f:
                self.session.cookies.update(json.load(f))
        self.validate_payload_config()

    def validate_payload_config(self):
        """Validate and fix payload configuration."""
        valid_states = ["success", "failed", "prepared"]
        if self.payload_config.get("client_prepare_state") not in valid_states:
            self.payload_config["client_prepare_state"] = "success"
        if self.payload_config.get("force_use_search") not in (True, False, None):
            self.payload_config["force_use_search"] = None
        bool_fields = ["history_and_training_disabled", "enable_message_followups",
                       "force_use_sse", "force_paragen", "supports_buffering",
                       "is_onboarding_conversation"]
        for f in bool_fields:
            if not isinstance(self.payload_config.get(f), bool):
                self.payload_config[f] = False
        self.save_state()

    def save_state(self):
        """Save state to config and cookies files."""
        data = {
            "device_id": self.device_id,
            "play_integrity_token": self.play_integrity_token,
            "conduit_token": self.conduit_token,
            "payload_config": self.payload_config,
            "base_url": self.base_url,
            "prepare_path": self.prepare_path,
            "sentinel_path": self.sentinel_path,
            "conversation_path": self.conversation_path,
            "user_agent": self.user_agent,
            "device_tier": self.device_tier,
            "account_id": self.account_id,
            "residency_region": self.residency_region,
            "accept_language": self.accept_language,
            "timezone": self.timezone,
            "timezone_offset": self.timezone_offset
        }
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        with open(self.cookies_file, "w") as f:
            json.dump(self.session.cookies.get_dict(), f, indent=2)

    def generate_sentry(self):
        """Generate sentry trace and baggage headers."""
        tid = uuid.uuid4().hex
        self.sentry_trace = f"{tid[:16]}-{tid[16:32]}"
        self.baggage = f"sentry-environment=production,sentry-org_id=33249,sentry-public_key=6884768431e4ba548d58cbf3ad96e4ce,sentry-release=com.openai.chatgpt%401.2026.195%2B2619512,sentry-sample_rand=0.{int(time.time()*1000)%1000000},sentry-trace_id={tid[:16]}"

    def common_headers(self):
        """Generate common headers for all requests."""
        self.generate_sentry()
        return {
            "host": self.base_url.replace("https://", ""),
            "user-agent": self.user_agent,
            "oai-package-name": "com.openai.chatgpt",
            "oai-client-type": "android",
            "oai-device-id": self.device_id,
            "accept-language": self.accept_language,
            "x-device-tier": self.device_tier,
            "chatgpt-account-id": self.account_id,
            "chatgpt-residency-region": self.residency_region,
            "accept": "application/json",
            "sentry-trace": self.sentry_trace,
            "baggage": self.baggage,
            "accept-encoding": "gzip"
        }

    def init_session(self):
        """Initialize session with prepare and sentinel endpoints."""
        self.convo_session_id = str(uuid.uuid4())
        self.turn_trace_id = str(uuid.uuid4())

        # Prepare
        url = f"{self.base_url}{self.prepare_path}"
        headers = {**self.common_headers(),
                   "x-oai-convo-session-id": self.convo_session_id,
                   "x-oai-turn-trace-id": self.turn_trace_id,
                   "x-conduit-token": self.conduit_token or "",
                   "x-openai-target-path": self.prepare_path,
                   "content-type": "application/json"}
        prepare_body = {
            "action":"next","messages":[],"model":self.payload_config["model"],
            "history_and_training_disabled":self.payload_config["history_and_training_disabled"],
            "fork_from_shared_post":False,"enable_message_followups":False,
            "force_use_sse":False,"force_use_search":None,"force_paragen":False,
            "supports_buffering":False,"timezone":self.timezone,
            "timezone_offset_min":self.timezone_offset,
            "system_hints":self.payload_config["system_hints"],
            "is_onboarding_conversation":self.payload_config["is_onboarding_conversation"],
            "no_auth_ad_preferences":self.payload_config["no_auth_ad_preferences"],
            "client_prepare_dispatch":self.payload_config["client_prepare_dispatch"],
            "client_prepare_source":self.payload_config["client_prepare_source"]
        }
        try:
            r = self.session.post(url, headers=headers, json=prepare_body)
            if r.ok and "conduit_token" in r.json():
                self.conduit_token = r.json()["conduit_token"]
                self.save_state()
        except: pass

        # Sentinel
        url2 = f"{self.base_url}{self.sentinel_path}"
        headers2 = {**self.common_headers(),
                    "x-openai-target-path": self.sentinel_path,
                    "content-type": "application/json"}
        try:
            r = self.session.post(url2, headers=headers2, json={})
            if r.ok:
                self.chat_req_token = r.json()["token"]
        except: pass
        self.save_state()

    def send_message(self, text, conversation_id=None, parent_id=None, on_token=None, retry=True):
        """Send a message to ChatGPT and get the response.
        
        Args:
            text: The message text to send
            conversation_id: Optional conversation ID for continuing conversations
            parent_id: Optional parent message ID for threading
            on_token: Optional callback function for streaming tokens
            retry: Whether to retry on auth errors (reinitializes session)
            
        Returns:
            Tuple of (reply_text, conversation_id, parent_id, model_used, error_message)
        """
        url = f"{self.base_url}{self.conversation_path}"
        sentinel = {"bot_token": {"play_integrity_token": self.play_integrity_token or "",
                                  "chat_requirement_token": self.chat_req_token or ""}}
        headers = {**self.common_headers(),
                   "accept":"text/event-stream,application/json",
                   "cache-control":"no-cache",
                   "x-sentinel-payload":json.dumps(sentinel),
                   "x-conduit-token":self.conduit_token or "",
                   "x-oai-convo-session-id":self.convo_session_id,
                   "x-oai-turn-trace-id":str(uuid.uuid4()),
                   "oai-echo-logs":"1,552,0,822,1,3296,1,5355,0,5533,1,8297,0,8739,1,9818,0,11081,1,12543",
                   "x-openai-target-path":self.conversation_path,
                   "content-type":"application/json"}
        msg_id = str(uuid.uuid4())
        body = {
            "action":"next",
            "messages":[{"id":msg_id,"author":{"role":"user"},
                          "content":{"parts":[text],"content_type":"text"},
                          "status":"finished_successfully","recipient":"all",
                          "metadata":{"model_slug":self.payload_config["model"],
                                     "default_model_slug":"auto"}}],
            "model":self.payload_config["model"],
            "history_and_training_disabled":self.payload_config["history_and_training_disabled"],
            "enable_message_followups":self.payload_config["enable_message_followups"],
            "force_use_sse":self.payload_config["force_use_sse"],
            "force_use_search":self.payload_config["force_use_search"],
            "force_paragen":self.payload_config["force_paragen"],
            "supports_buffering":self.payload_config["supports_buffering"],
            "timezone":self.timezone,
            "timezone_offset_min":self.timezone_offset,
            "system_hints":self.payload_config["system_hints"],
            "is_onboarding_conversation":self.payload_config["is_onboarding_conversation"],
            "no_auth_ad_preferences":self.payload_config["no_auth_ad_preferences"],
            "client_prepare_state":self.payload_config["client_prepare_state"],
            "stream":True
        }
        if conversation_id: body["conversation_id"] = conversation_id
        if parent_id: body["parent_message_id"] = parent_id

        try:
            r = self.session.post(url, headers=headers, json=body, stream=True)
            if r.status_code in (401, 403, 422, 500) and retry:
                self.init_session()
                return self.send_message(text, conversation_id, parent_id, on_token, False)
            r.raise_for_status()
        except Exception as e:
            error_msg = f"Exception: {e}\nStatus: {r.status_code}\nBody: {json.dumps(body, indent=2, ensure_ascii=False)[:1000]}"
            return None, None, None, None, error_msg

        if "x-conduit-token" in r.headers:
            self.conduit_token = r.headers["x-conduit-token"]
            self.save_state()

        full_text = ""
        new_conv = conversation_id
        new_parent = parent_id
        model_used = self.payload_config["model"]

        try:
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "): continue
                data = line[6:]
                if data == "[DONE]": break
                try: ev = json.loads(data)
                except: continue
                if ev.get("type") == "resume_conversation_token":
                    new_conv = ev.get("conversation_id", new_conv)
                if "message" in ev:
                    m = ev["message"]
                    if m["author"]["role"] == "assistant" and m.get("channel") == "final":
                        new_parent = m["id"]
                        if "metadata" in m and "model_slug" in m["metadata"]:
                            model_used = m["metadata"]["model_slug"]
                        parts = m["content"]["parts"]
                        if parts:
                            cur = "".join(parts)
                            if cur != full_text:
                                new_part = cur[len(full_text):]
                                full_text = cur
                                if on_token: on_token(new_part)
        except Exception as e:
            error_msg = f"Stream Error: {e}"
            return full_text or None, new_conv, new_parent, model_used, error_msg

        self.save_state()
        return full_text, new_conv, new_parent, model_used, None
