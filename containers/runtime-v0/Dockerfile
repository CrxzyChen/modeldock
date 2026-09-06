# Runtime-v0 is the compatibility core for the locked Python 3.10 / CUDA 12.1 era.
# Every archive, wheel and deb is supplied by a release-prepared offline context.
FROM runtime_parent AS compile
ENV DEBIAN_FRONTEND=noninteractive SOURCE_DATE_EPOCH=1786406400
COPY build-debs/ /offline/debs/
COPY Python-3.10.20.tar.xz /offline/Python-3.10.20.tar.xz
COPY wheels/ /offline/wheels/
COPY requirements.txt /offline/requirements.txt
RUN dpkg -i /offline/debs/*.deb
WORKDIR /build
RUN tar -xJf /offline/Python-3.10.20.tar.xz --strip-components=1 \
    && ./configure --prefix=/opt/python --enable-shared --with-ensurepip=no \
    && make -j2 && make install
ENV PATH=/opt/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LD_LIBRARY_PATH=/opt/python/lib PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN PYTHONPATH=/offline/wheels/pip-26.2.1-py3-none-any.whl \
    python3.10 -m pip install --no-index --no-deps --require-hashes \
    --find-links=/offline/wheels -r /offline/requirements.txt \
    && python3.10 -m pip check

FROM runtime_parent AS runtime
COPY runtime-debs/ /offline/runtime-debs/
RUN dpkg -i /offline/runtime-debs/*.deb
COPY --from=compile /opt/python/ /opt/python/
COPY --chown=1000:1000 sdk/ /opt/mediacenter/
ENV PATH=/opt/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LD_LIBRARY_PATH=/opt/python/lib PYTHONPATH=/opt/mediacenter \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_INDEX=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 HOME=/tmp
USER 1000:1000
WORKDIR /opt/mediacenter
RUN python3.10 -m pip check && python3.10 -B -c \
    "import torch,torchaudio,diffusers,transformers,onnx,onnxruntime,numpy,redis; import mediacenter.worker_runtime"
CMD ["python3.10"]
