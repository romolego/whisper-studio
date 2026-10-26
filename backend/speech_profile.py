"""Decode speech turns independently so distant utterances are not spliced."""
from types import SimpleNamespace
from faster_whisper.audio import decode_audio
from faster_whisper.vad import VadOptions, get_speech_timestamps
from . import cancellation, gpu_turns


def transcribe(model, path, options: dict, job_id: str):
    cancellation.check(job_id)
    audio = decode_audio(str(path))
    parameters = dict(options.get("vad_parameters") or {})
    parameters["max_speech_duration_s"] = 26
    clips = get_speech_timestamps(audio, VadOptions(**parameters))
    cancellation.check(job_id)
    kwargs = {key: value for key, value in options.items() if key not in {"vad_filter", "vad_parameters"}}
    kwargs["vad_filter"] = False
    duration = len(audio) / 16000
    if not clips:
        return iter(()), SimpleNamespace(language=options.get("language") or "unknown",
                                        language_probability=0.0, duration=duration)
    first = clips[0]
    first_segments, info = model.transcribe(audio[first["start"]:first["end"]], **kwargs)

    def generate():
        for index, clip in enumerate(clips):
            cancellation.check(job_id); gpu_turns.checkpoint()
            segments = first_segments if index == 0 else model.transcribe(audio[clip["start"]:clip["end"]], **kwargs)[0]
            shift = clip["start"] / 16000
            for segment in segments:
                words = [SimpleNamespace(start=word.start+shift, end=word.end+shift,
                                         word=word.word, probability=word.probability)
                         for word in segment.words or []]
                yield SimpleNamespace(start=segment.start+shift, end=segment.end+shift,
                                      text=segment.text, words=words)
    return generate(), SimpleNamespace(language=info.language, language_probability=info.language_probability,
                                       duration=duration,
                                       duration_after_vad=sum(c["end"]-c["start"] for c in clips)/16000)
