from datetime import datetime

from ..exceptions import AnymailRequestsAPIError, AnymailError
from ..message import AnymailRecipientStatus
from ..utils import get_anymail_setting, rfc2822date

from .base_requests import AnymailRequestsBackend, RequestsPayload


class MailgunBackend(AnymailRequestsBackend):
    """
    Mailgun API Email Backend
    """

    def __init__(self, **kwargs):
        """Init options from Django settings"""
        self.api_key = get_anymail_setting('MAILGUN_API_KEY', allow_bare=True)
        api_url = get_anymail_setting("MAILGUN_API_URL", "https://api.mailgun.net/v3")
        if not api_url.endswith("/"):
            api_url += "/"
        super(MailgunBackend, self).__init__(api_url, **kwargs)

    def build_message_payload(self, message, defaults):
        return MailgunPayload(message, defaults, self)

    def parse_recipient_status(self, response, payload, message):
        # The *only* 200 response from Mailgun seems to be:
        #     {
        #       "id": "<20160306015544.116301.25145@example.org>",
        #       "message": "Queued. Thank you."
        #     }
        #
        # That single message id applies to all recipients.
        # The only way to detect rejected, etc. is via webhooks.
        # (*Any* invalid recipient addresses will generate a 400 API error)
        parsed_response = self.deserialize_json_response(response, payload, message)
        try:
            message_id = parsed_response["id"]
            mailgun_message = parsed_response["message"]
        except (KeyError, TypeError):
            raise AnymailRequestsAPIError("Invalid Mailgun API response format",
                                          email_message=message, payload=payload, response=response)
        if not mailgun_message.startswith("Queued"):
            raise AnymailRequestsAPIError("Unrecognized Mailgun API message '%s'" % mailgun_message,
                                          email_message=message, payload=payload, response=response)
        # Simulate a per-recipient status of "queued":
        status = AnymailRecipientStatus(message_id=message_id, status="queued")
        return {recipient.email: status for recipient in payload.all_recipients}


class MailgunPayload(RequestsPayload):

    def __init__(self, message, defaults, backend, *args, **kwargs):
        auth = ("api", backend.api_key)
        self.sender_domain = None
        self.all_recipients = []  # used for backend.parse_recipient_status
        super(MailgunPayload, self).__init__(message, defaults, backend, auth=auth, *args, **kwargs)

    def get_api_endpoint(self):
        if self.sender_domain is None:
            raise AnymailError("Cannot call Mailgun unknown sender domain. "
                               "Either provide valid `from_email`, "
                               "or set `message.esp_extra={'sender_domain': 'example.com'}`",
                               backend=self.backend, email_message=self.message, payload=self)
        return "%s/messages" % self.sender_domain

    #
    # Payload construction
    #

    def init_payload(self):
        self.data = {}   # {field: [multiple, values]}
        self.files = []  # [(field, multiple), (field, values)]

    def set_from_email(self, email):
        self.data["from"] = str(email)
        if self.sender_domain is None:
            # try to intuit sender_domain from from_email
            try:
                _, domain = email.email.split('@')
                self.sender_domain = domain
            except ValueError:
                pass

    def set_recipients(self, recipient_type, emails):
        assert recipient_type in ["to", "cc", "bcc"]
        if emails:
            self.data[recipient_type] = [str(email) for email in emails]
            self.all_recipients += emails  # used for backend.parse_recipient_status

    def set_subject(self, subject):
        self.data["subject"] = subject

    def set_reply_to(self, emails):
        if emails:
            reply_to = ", ".join([str(email) for email in emails])
            self.data["h:Reply-To"] = reply_to

    def set_extra_headers(self, headers):
        for key, value in headers.items():
            self.data["h:%s" % key] = value

    def set_text_body(self, body):
        self.data["text"] = body

    def set_html_body(self, body):
        if "html" in self.data:
            # second html body could show up through multiple alternatives, or html body + alternative
            self.unsupported_feature("multiple html parts")
        self.data["html"] = body

    def add_attachment(self, attachment):
        # http://docs.python-requests.org/en/v2.4.3/user/advanced/#post-multiple-multipart-encoded-files
        field = "inline" if attachment.inline else "attachment"
        self.files.append(
            (field, (attachment.name, attachment.content, attachment.mimetype))
        )

    def set_metadata(self, metadata):
        # The Mailgun docs are a little unclear on whether to send each var as a separate v: field,
        # or to send a single 'v:my-custom-data' field with a json blob of all vars.
        # (https://documentation.mailgun.com/user_manual.html#attaching-data-to-messages)
        # From experimentation, it seems like the first option works:
        for key, value in metadata.items():
            # Ensure the value is json-serializable (for Mailgun storage)
            json = self.serialize_json(value)  # will raise AnymailSerializationError
            # Special case: a single string value should be sent bare (without quotes),
            # because Mailgun will add quotes when querying the value as json.
            if json.startswith('"'):  # only a single string could be serialized as "...
                json = value
            self.data["v:%s" % key] = json

    def set_send_at(self, send_at):
        # Mailgun expects RFC-2822 format dates
        # (BasePayload has converted most date-like values to datetime by now;
        # if the caller passes a string, they'll need to format it themselves.)
        if isinstance(send_at, datetime):
            send_at = rfc2822date(send_at)
        self.data["o:deliverytime"] = send_at

    def set_tags(self, tags):
        self.data["o:tag"] = tags

    def set_track_clicks(self, track_clicks):
        # Mailgun also supports an "htmlonly" option, which Anymail doesn't offer
        self.data["o:tracking-clicks"] = "yes" if track_clicks else "no"

    def set_track_opens(self, track_opens):
        self.data["o:tracking-opens"] = "yes" if track_opens else "no"

    def set_esp_extra(self, extra):
        self.data.update(extra)
        # Allow override of sender_domain via esp_extra
        # (but pop it out of params to send to Mailgun)
        self.sender_domain = self.data.pop("sender_domain", self.sender_domain)