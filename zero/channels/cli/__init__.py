"""CLI channel package.

Public surface: ``CLIChannel`` and its channel name. Everything else in this
package (renderer, input, commands, approval, background) is an internal
collaborator of the interactive terminal transport.
"""
from zero.channels.cli.channel import CHANNEL, CLIChannel

__all__ = ["CHANNEL", "CLIChannel"]
