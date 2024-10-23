from flask import Flask, request, jsonify
import os
from dotenv import load_dotenv
import requests
from google.cloud import dialogflowcx_v3 as dialogflowcx
from google.oauth2 import service_account
from pathlib import Path

dotenv_path = Path('config/.env')
load_dotenv(dotenv_path=dotenv_path)

app = Flask(__name__)

# Load environment variables
project_id = os.environ.get('PROJECT_ID')
location = os.environ.get('LOCATION', 'us-central1')  # e.g., 'us-central1'
agent_id = os.environ.get('AGENT_ID')
google_application_credential = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
chatwoot_api_key = os.environ.get('CHATWOOT_API_KEY')
chatwoot_url = os.environ.get('CHATWOOT_URL')

# Setup Google Dialogflow CX credentials
credentials = service_account.Credentials.from_service_account_file(
    google_application_credential
)

# Create Dialogflow CX client
dialogflow_client = dialogflowcx.SessionsClient(credentials=credentials)

# Chatwoot Webhook route
@app.route('/chatwoot-webhook', methods=['POST'])
def chatwoot_webhook():
    request_data = request.get_json()
    message = request_data['content']
    sender_id = request_data['sender']['id']

    # Send message to Dialogflow CX
    session_id = f"session_{sender_id}"
    response_text = send_message_to_dialogflow_cx(session_id, message)

    # Send reply back to Chatwoot
    send_reply_to_chatwoot(sender_id, response_text)

    return jsonify({"status": "success", "response": response_text}), 200

def send_message_to_dialogflow_cx(session_id, message):
    # Construct session path
    session_path = dialogflow_client.session_path(project_id, location, agent_id, session_id)

    # Specify the language code here
    language_code = 'pt-br'  # or set dynamically if needed

    # Create text input and query input
    text_input = dialogflowcx.TextInput(text=message)
    query_input = dialogflowcx.QueryInput(text=text_input, language_code=language_code)

    # Make the request to Dialogflow CX
    response = dialogflow_client.detect_intent(
        request={"session": session_path, "query_input": query_input}
    )

    # Accessing the first response message text
    if response.query_result.response_messages:
        fulfillment_text = response.query_result.response_messages[0].text.text[0]
    else:
        fulfillment_text = "Desculpe, não entendi."

    return fulfillment_text

def send_reply_to_chatwoot(sender_id, response_message):
    url = f"{chatwoot_url}/api/v1/conversations/{sender_id}/messages"
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {chatwoot_api_key}'
    }
    payload = {
        "content": response_message,
        "message_type": "outgoing"
    }

    response = requests.post(url, headers=headers, json=payload)
    return response.text

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)