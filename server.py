"""
WhatsApp Weather + Commute Bot
Receives incoming WhatsApp messages via Twilio webhook,
parses "now" or time strings, and replies with weather + commute report.
"""

from flask import Flask, request, Response
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import sys
import os

from weather_commute import build_message

app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = "whatsapp:+14155238886"
YOUR_WHATSAPP_NUMBER = "whatsapp:+19146562995"
GOOGLE_MAPS_KEY = os.environ.get("GOOGLE_MAPS_KEY", "")
BART_KEY = os.environ.get("BART_KEY", "")

client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID else None


@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    from_number = request.form.get("From", "")

    print(f"Incoming from {from_number}: {incoming_msg}")

    # Only respond to your own number
    if from_number != YOUR_WHATSAPP_NUMBER:
        return Response("Unauthorized", status=403)

    try:
        report = build_message(incoming_msg, GOOGLE_MAPS_KEY, BART_KEY)
    except ValueError as e:
        report = f"Sorry, I couldn't parse that time. Try 'now', '9AM', '9:00AM', or '10P'.\n\nError: {e}"
    except Exception as e:
        report = f"Something went wrong fetching the report. Error: {e}"

    resp = MessagingResponse()
    resp.message(report)
    return Response(str(resp), mimetype="application/xml")


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
