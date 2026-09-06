# Three fixed derived targets share the verified runtime-v1 filesystem layers.
# The build context must provide the locked wheel sets named below; network is
# disabled and no target is selected from task input.
FROM runtime_parent AS common
USER 0:0
COPY --chown=1000:1000 sdk/ /opt/mediacenter/

FROM common AS sdxl
COPY locked/sdxl/ /offline/image-wheels/
COPY locked/sdxl.requirements.txt /offline/image.requirements.txt
RUN /opt/python/bin/python3.11 -m pip install --no-index --no-deps --require-hashes \
    --find-links=/offline/image-wheels -r /offline/image.requirements.txt
USER 1000:1000
RUN /opt/python/bin/python3.11 -m pip check && /opt/python/bin/python3.11 -B -c \
    "import mediacenter.image_worker_cli,mediacenter.adapters.illustrious"
ENTRYPOINT ["/opt/python/bin/python3.11","-B","-u","-m","mediacenter.image_worker_cli"]

FROM common AS zimage
COPY locked/zimage/ /offline/image-wheels/
COPY locked/zimage.requirements.txt /offline/image.requirements.txt
RUN /opt/python/bin/python3.11 -m pip install --no-index --no-deps --require-hashes \
    --find-links=/offline/image-wheels -r /offline/image.requirements.txt
USER 1000:1000
RUN /opt/python/bin/python3.11 -m pip check && /opt/python/bin/python3.11 -B -c \
    "import mediacenter.image_worker_cli,mediacenter.adapters.zimage"
ENTRYPOINT ["/opt/python/bin/python3.11","-B","-u","-m","mediacenter.image_worker_cli"]

FROM common AS qwen
COPY locked/qwen/ /offline/image-wheels/
COPY locked/qwen.requirements.txt /offline/image.requirements.txt
RUN /opt/python/bin/python3.11 -m pip install --no-index --no-deps --require-hashes \
    --find-links=/offline/image-wheels -r /offline/image.requirements.txt
USER 1000:1000
RUN /opt/python/bin/python3.11 -m pip check && /opt/python/bin/python3.11 -B -c \
    "import mediacenter.image_worker_cli,mediacenter.adapters.qwen_image"
ENTRYPOINT ["/opt/python/bin/python3.11","-B","-u","-m","mediacenter.image_worker_cli"]

FROM common AS upscale
USER 1000:1000
RUN /opt/python/bin/python3.11 -m pip check && /opt/python/bin/python3.11 -B -c \
    "import mediacenter.image_worker_cli,mediacenter.adapters.realesrgan"
ENTRYPOINT ["/opt/python/bin/python3.11","-B","-u","-m","mediacenter.image_worker_cli"]
