"""音频测试夹具：生成合法 PCM WAV，不依赖真实引擎/网络。"""
import io
import struct
import wave


def make_wav(seconds=0.2, rate=24000, channels=1, freq=440.0, amplitude=8000) -> bytes:
    frames = max(1, int(rate * seconds))
    samples = []
    for i in range(frames):
        value = int(amplitude * (i * freq / rate % 1.0 * 2 - 1))
        samples.extend([value] * channels)
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(struct.pack('<%dh' % len(samples), *samples))
    return buffer.getvalue()


def wav_duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data), 'rb') as stream:
        return stream.getnframes() / float(stream.getframerate())
