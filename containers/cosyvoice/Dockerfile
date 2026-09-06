# CosyVoice derivative adds immutable application source/reference bytes only.
FROM runtime_v0_parent
COPY --chown=1000:1000 sources/CosyVoice/ /opt/cosyvoice/
COPY --chown=1000:1000 sources/Matcha-TTS/ /opt/cosyvoice/third_party/Matcha-TTS/
COPY --chown=1000:1000 sdk/ /opt/mediacenter/
USER 1000:1000
WORKDIR /opt/mediacenter
RUN /opt/python/bin/python3.10 -m pip check && /opt/python/bin/python3.10 -B -c \
    "import torch,torchaudio,onnxruntime; import mediacenter.audio_worker_cli; import mediacenter.adapters.cosyvoice"
ENTRYPOINT ["/opt/python/bin/python3.10","-B","-u","-m","mediacenter.audio_worker_cli"]
