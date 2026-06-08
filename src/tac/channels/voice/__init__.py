"""Voice channel for handling voice-based conversations."""

from tac.channels.voice.channel import VoiceChannel
from tac.channels.voice.config import VoiceChannelConfig
from tac.channels.voice.twiml import generate_twiml

__all__ = ["VoiceChannel", "VoiceChannelConfig", "generate_twiml"]
