import json

from flask import Flask, request, jsonify
import os
from dotenv import load_dotenv
import requests
from google.cloud import dialogflowcx_v3 as dialogflow
from google.oauth2 import service_account
from pathlib import Path
import logging
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Struct

dotenv_path = Path('config/.env')
load_dotenv(dotenv_path=dotenv_path)

app = Flask(__name__)

# Set up logging, show debug logs only if in DEBUG mode
if app.config['DEBUG']:
    logging.basicConfig(level=logging.DEBUG)
else:
    logging.basicConfig(level=logging.INFO)

# Load environment variables
project_id = os.environ.get('PROJECT_ID')
location = os.environ.get('LOCATION', 'us-central1')
agent_id = os.environ.get('AGENT_ID')
google_application_credential = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
chatwoot_api_key = os.environ.get('CHATWOOT_API_KEY')
chatwoot_url = os.environ.get('CHATWOOT_URL')

# Setup Google Dialogflow CX credentials
credentials = service_account.Credentials.from_service_account_file(
    google_application_credential
)

# Create Dialogflow CX client
dialogflow_client = dialogflow.SessionsClient(credentials=credentials)

# Chatwoot Webhook route
@app.route('/chatwoot-webhook', methods=['POST'])
def chatwoot_webhook():
    request_data = request.get_json()
    app.logger.debug(f"Received request data: {request_data}")

    # Validate input data
    if not request_data or 'content' not in request_data:
        app.logger.error("Key 'content' not found in request data.")
        return jsonify({"status": "error", "message": "Invalid request data"}), 400

    # Extract relevant fields
    message_type = request_data.get('message_type')
    message = request_data.get('content')
    conversation = request_data.get('conversation', {}).get('id')
    conversation_status = request_data.get('conversation', {}).get('status')
    sender_id = request_data.get('sender', {}).get('id')
    account = request_data.get('account', {}).get('id')

    custom_attributes = {}

    # Verify on custom user attribute from Chatwoot if the user meta was already sent to DialogFlow
    user_meta_sent_dialogflow = request_data.get('conversation', {}).get('custom_attributes', {}).get(
        'user_meta_sent_dialogflow')

    contact_info = {
        'contact_id': request_data.get('sender', {}).get('id'),
        'contact_name': request_data.get('sender', {}).get('name'),
        'contact_phone': request_data.get('sender', {}).get('phone_number'),
        'email': request_data.get('sender', {}).get('email')
    }

    if not user_meta_sent_dialogflow:
        if contact_info['contact_name']:
            message += "Meu nome é " + contact_info['contact_name'] + " "
        if contact_info['contact_phone']:
            message += "Meu telefone é " + contact_info['contact_phone'] + " "
        if contact_info['email']:
            message += "Meu email é " + contact_info['email'] + " "

        custom_attributes['user_meta_sent_dialogflow'] = True
        add_custom_attributes_chatwoot_conversation(account, conversation, custom_attributes)


    if message_type == 'incoming' and sender_id and conversation_status == 'pending':
        # Send message to Dialogflow CX
        session_id = f"session_{sender_id}"
        response_text, end_interaction = send_message_to_dialogflow_cx(session_id, message, request_data)
        app.logger.debug(f"Dialogflow function response: {response_text}, {end_interaction}")

        if not end_interaction:
            # Send reply back to Chatwoot
            send_reply_to_chatwoot(account, conversation, response_text)
        # If end_interaction is true
        else:
            # Send the execution summary as a private message on Chatwoot
            send_reply_to_chatwoot(account, conversation, response_text, True)
            # Set conversation status to "open" for human agent intervention
            update_chatwoot_conversation_status(account, conversation, 'open')

    return jsonify({"status": "success"}), 200


def send_message_to_dialogflow_cx(session_id, message, request_data=None):
    if request_data is None:
        request_data = []

    session_path = dialogflow_client.session_path(project_id, location, agent_id, session_id)
    language_code = 'pt-br'

    # Prepare the text input for Dialogflow
    text_input = dialogflow.TextInput(text=message)
    query_input = dialogflow.QueryInput(text=text_input, language_code=language_code)

    parameters_json = {
            # "content": request_data.get('content'),
            "contact_info": {
                "contact_id": request_data.get('sender', {}).get('id'),
                "contact_name": request_data.get('sender', {}).get('name'),
                "contact_phone": request_data.get('sender', {}).get('phone_number'),
                "email": request_data.get('sender', {}).get('email')
            },
            "browser_info": request_data.get('conversation', {}).get('additional_attributes', {}).get('browser',
                                                                                                      {}),
    }

    # Prepare the query parameters
    query_parameters = dialogflow.QueryParameters(
        # time_zone=request_data.get('conversation', {}).get('additional_attributes', {}).get('browser', {}).get(
        #    'timezone', 'America/Sao_Paulo'),  # default time zone
        # geo_location=dialogflowcx.types.LatLng(
        #     latitude=request_data.get('additional_attributes', {}).get('latitude', 0.0),
        #     longitude=request_data.get('additional_attributes', {}).get('longitude', 0.0)
        # ),
        end_user_metadata=parameters_json,
        analyze_query_text_sentiment=True,
        parameters=parameters_json
    )

    request = dialogflow.DetectIntentRequest(
        session=session_path,
        query_input=query_input,
        query_params=query_parameters
    )
    # Make the request to Dialogflow CX
    response = dialogflow_client.detect_intent(
        request=request
    )

    # Convert response to a dictionary
    response_dict = json_format.MessageToDict(response._pb)

    # Default values
    end_interaction = False
    fulfillment_text = "Desculpe, não entendi."

    # Check if `parameters` and `fields` are present before accessing
    if 'parameters' in response_dict['queryResult'] and 'execution_summary' in response_dict['queryResult']['parameters']:
        fulfillment_text = response_dict['queryResult']['parameters'].get("execution_summary")

    # Handle response messages
    if 'responseMessages' in response_dict['queryResult']:
        first_message = response_dict['queryResult']['responseMessages'][0]

        # Check if the text field exists and contains text
        if 'text' in first_message and 'text' in first_message['text']:
            response_text = first_message['text']['text'][0]
        else:
            response_text = fulfillment_text

        # Check if end_interaction is specified
        if 'endInteraction' in first_message:
            end_interaction = True
    else:
        response_text = fulfillment_text

    return response_text, end_interaction

def send_reply_to_chatwoot(account, conversation, response_message, private=False):
    private = bool(private)

    url = f"{chatwoot_url}/api/v1/accounts/{account}/conversations/{conversation}/messages"
    headers = {
        'Content-Type': 'application/json',
        'api_access_token': f'{chatwoot_api_key}'
    }
    payload = {
        "content": response_message,
        "message_type": "outgoing",
        "private": private,
    }

    response = requests.post(url, headers=headers, json=payload)
    return response.text

def add_custom_attributes_chatwoot_conversation(account, conversation, custom_attributes):
    valid_attributes = {key: value for key, value in custom_attributes.items() if value}

    if not valid_attributes:
        return

    url = f"{chatwoot_url}/api/v1/accounts/{account}/conversations/{conversation}/custom_attributes"
    headers = {
        'Content-Type': 'application/json',
        'api_access_token': chatwoot_api_key
    }

    payload = {"custom_attributes": valid_attributes}

    response = requests.post(url, headers=headers, json=payload)
    app.logger.info(f"Added custom attributes to Chatwoot for conversation {conversation}.")
    return response.text

def update_chatwoot_conversation_status(account, conversation, status):
    url = f"{chatwoot_url}/api/v1/accounts/{account}/conversations/{conversation}/toggle_status"
    headers = {
        'Content-Type': 'application/json',
        'api_access_token': f'{chatwoot_api_key}'
    }
    payload = {
        "status": status
    }

    response = requests.post(url, headers=headers, json=payload)
    app.logger.info(f"Updated Chatwoot conversation status to '{status}' for conversation {conversation}.")
    return response.text

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
