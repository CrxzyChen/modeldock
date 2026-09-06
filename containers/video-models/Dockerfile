# Fixed common video-model derivative.  Every wheel is supplied by the
# independently prepared offline wheelhouse; build-time resolution is disabled.
FROM runtime_parent
USER 0:0
COPY locked/ /offline/video-model-wheels/
COPY requirements.txt /offline/video-model-requirements.txt
RUN /opt/python/bin/python3.11 -m pip install --no-index --no-deps --require-hashes \
    --find-links=/offline/video-model-wheels -r /offline/video-model-requirements.txt
COPY --chown=1000:1000 sdk/ /opt/mediacenter/
USER 1000:1000
WORKDIR /opt/mediacenter
RUN /opt/python/bin/python3.11 -m pip check && /opt/python/bin/python3.11 -B -c \
    "import av,diffusers,ftfy,imageio,imageio_ffmpeg,torchao,transformers,wcwidth; import mediacenter.video_worker_cli; import mediacenter.adapters.h3,mediacenter.adapters.ltx,mediacenter.adapters.wan22,mediacenter.adapters.hunyuan15"
ENTRYPOINT ["/opt/python/bin/python3.11","-B","-u","-m","mediacenter.video_worker_cli"]
