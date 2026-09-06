# MusicGen derivative reuses the complete Runtime-v1 Python closure.
FROM runtime_parent
COPY --chown=1000:1000 sdk/ /opt/mediacenter/
USER 1000:1000
WORKDIR /opt/mediacenter
RUN /opt/python/bin/python3.11 -m pip check && /opt/python/bin/python3.11 -B -c \
    "import numpy,torch,transformers; import mediacenter.audio_worker_cli; import mediacenter.adapters.musicgen"
ENTRYPOINT ["/opt/python/bin/python3.11","-B","-u","-m","mediacenter.audio_worker_cli"]
