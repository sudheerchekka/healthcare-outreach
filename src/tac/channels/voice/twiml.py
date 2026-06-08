"""TwiML generation for voice channel."""

from typing import Any

from pydantic import BaseModel
from twilio.twiml.voice_response import VoiceResponse

from tac.models.voice import TwiMLOptions


def generate_twiml(
    options: TwiMLOptions | dict[str, Any],
) -> str:
    """
    Generate TwiML XML for ConversationRelay with custom parameters.

    This is a low-level function for generating TwiML with arbitrary custom
    parameters. For automatic conversation creation and participant management,
    use VoiceChannel.handle_incoming_call() instead.

    Args:
        options: TwiML generation options (TwiMLOptions model or dict with:
            - websocket_url (required): WebSocket URL for ConversationRelay
            - custom_parameters (optional): Dict of custom parameters
            - welcome_greeting (optional): Initial greeting message
            - action_url (optional): URL for call end webhook
            - conversation_configuration (optional): Conversation Service SID for
              automatic conversation creation

    Returns:
        TwiML XML string ready to return to Twilio

    Example:
        >>> twiml = generate_twiml(
        ...     {
        ...         "websocket_url": "wss://example.com/voice",
        ...         "custom_parameters": {
        ...             "session_id": "sess_abc123",
        ...             "user_language": "es",
        ...         },
        ...         "welcome_greeting": "Hello!",
        ...         "conversation_configuration": "conv_configuration_xxxx",
        ...     }
        ... )
    """
    # Handle dict input (convert to TwiMLOptions)
    if isinstance(options, dict):
        options = TwiMLOptions(**options)

    websocket_url = options.websocket_url
    custom_parameters = options.custom_parameters
    welcome_greeting = options.welcome_greeting
    action_url = options.action_url
    conversation_configuration = options.conversation_configuration

    # Create VoiceResponse
    response = VoiceResponse()

    # Create Connect verb with optional action
    connect_kwargs: dict[str, str] = {}
    if action_url:
        connect_kwargs["action"] = action_url
    connect = response.connect(**connect_kwargs)

    # Build ConversationRelay kwargs
    relay_kwargs: dict[str, str] = {"url": websocket_url}
    if welcome_greeting:
        relay_kwargs["welcome_greeting"] = welcome_greeting
    if conversation_configuration:
        relay_kwargs["conversation_configuration"] = conversation_configuration

    # Create ConversationRelay
    relay = connect.conversation_relay(**relay_kwargs)

    # Add custom parameters
    if custom_parameters:
        # Handle both Pydantic model and dict
        params_dict: dict[str, Any] = (
            custom_parameters.model_dump(by_alias=True, exclude_none=True)
            if isinstance(custom_parameters, BaseModel)
            else custom_parameters
        )

        # Add each parameter as a child element
        for name, value in params_dict.items():
            if value is not None:
                relay.parameter(name=name, value=str(value))

    return str(response)
