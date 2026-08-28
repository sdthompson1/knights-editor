"""Sound preview playback for the Lua state browser.

The game (Coercri SDLSoundDriver) ignores a WAV file's own sample rate:
kts.PlaySound(pos, sound, frequency) steps through the raw samples at
`frequency` Hz. This module reproduces that by handing the file's PCM
data to a QAudioSink whose QAudioFormat carries the chosen rate.
"""

import wave

from PySide6.QtCore import (
    QBuffer, QByteArray, QIODevice, QObject, Signal, Slot,
)
from PySide6.QtMultimedia import (
    QAudioFormat, QAudioSink, QMediaDevices, QtAudio,
)


class WavData:
    """The decoded contents of one PCM WAV file.

    Raises (wave.Error, OSError, EOFError) if the file is missing or is
    not plain PCM.
    """

    def __init__(self, filename: str):
        with wave.open(filename, 'rb') as w:
            self.channels = w.getnchannels()
            self.sample_width = w.getsampwidth()   # bytes per sample
            self.native_rate = w.getframerate()
            self.n_frames = w.getnframes()
            self.pcm = w.readframes(self.n_frames)


class SoundPlayer(QObject):
    """Plays one WavData at a time through the default audio output.

    QAudioSink.start() is asynchronous on most backends, so device
    errors usually surface after play() has returned; they are reported
    through `playback_error`. The sink is released as soon as the sound
    finishes (the sink itself only goes idle, keeping the device open).
    """

    playback_error = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._sink: QAudioSink | None = None
        self._buffer: QBuffer | None = None

    def play(self, wav: WavData, frequency: int) -> str | None:
        """Start playing `wav` at `frequency` Hz (replacing any current
        playback). Returns an error message for problems detected up
        front, or None if playback started; errors that the backend
        reports later arrive via `playback_error`."""
        self.stop()
        fmt = QAudioFormat()
        fmt.setSampleRate(int(frequency))
        fmt.setChannelCount(wav.channels)
        if wav.sample_width == 1:
            fmt.setSampleFormat(QAudioFormat.SampleFormat.UInt8)
        elif wav.sample_width == 2:
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        else:
            return f"unsupported sample size ({wav.sample_width * 8}-bit)"
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            return "no audio output device"
        if not device.isFormatSupported(fmt):
            return (f"{frequency} Hz not supported by the audio "
                    "output device")
        sink = QAudioSink(device, fmt)
        buf = QBuffer()
        buf.setData(QByteArray(wav.pcm))
        buf.open(QIODevice.OpenModeFlag.ReadOnly)
        self._sink, self._buffer = sink, buf
        sink.stateChanged.connect(self._on_state_changed)
        sink.start(buf)
        # Some backends do fail synchronously; catch those here so the
        # caller gets an immediate message.
        if sink.error() != QtAudio.Error.NoError:
            msg = self._error_message(sink)
            self.stop()
            return msg
        return None

    def stop(self):
        sink, self._sink = self._sink, None
        buf, self._buffer = self._buffer, None
        if sink is not None:
            # Disconnect first: sink.stop() emits stateChanged itself.
            sink.stateChanged.disconnect(self._on_state_changed)
            sink.stop()
            sink.deleteLater()
        if buf is not None:
            buf.close()
            buf.deleteLater()

    @Slot(QtAudio.State)
    def _on_state_changed(self, state):
        sink = self._sink
        if sink is None:
            return
        if state == QtAudio.State.IdleState:
            # Buffer exhausted: the sound has finished. Release the
            # device rather than leaving the sink parked on it.
            self.stop()
        elif (state == QtAudio.State.StoppedState
              and sink.error() != QtAudio.Error.NoError):
            msg = self._error_message(sink)
            self.stop()
            self.playback_error.emit(msg)

    @staticmethod
    def _error_message(sink: QAudioSink) -> str:
        return f"audio error ({sink.error().name})"
