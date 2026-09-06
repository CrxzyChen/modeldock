# No external syntax/frontend. runtime_parent is a fully verified OCI-layout
# named context supplied by the fixed remote BuildKit invocation, never a tag.
FROM runtime_parent AS compile
ENV DEBIAN_FRONTEND=noninteractive SOURCE_DATE_EPOCH=1786406400
COPY build-debs/ /offline/debs/
COPY Python-3.11.15.tar.xz /offline/Python-3.11.15.tar.xz
COPY wheels/ /offline/wheels/
COPY requirements.txt /offline/requirements.txt
RUN dpkg -i /offline/debs/*.deb
WORKDIR /build
RUN tar -xJf /offline/Python-3.11.15.tar.xz --strip-components=1 \
    && ./configure --prefix=/opt/python --enable-shared --with-ensurepip=no \
    && make -j2 && make install
ENV PATH=/opt/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LD_LIBRARY_PATH=/opt/python/lib \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN PYTHONPATH=/offline/wheels/pip-25.1.1-py3-none-any.whl \
    python3.11 -m pip install --no-index --no-deps --require-hashes \
    --find-links=/offline/wheels -r /offline/requirements.txt \
    && python3.11 -m pip check

FROM runtime_parent AS runtime
ENV DEBIAN_FRONTEND=noninteractive
COPY runtime-debs/ /offline/runtime-debs/
RUN dpkg -i /offline/runtime-debs/*.deb
COPY --from=compile /opt/python/ /opt/python/
COPY --chown=1000:1000 sdk/ /opt/mediacenter/
ENV PATH=/opt/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LD_LIBRARY_PATH=/opt/python/lib PYTHONPATH=/opt/mediacenter \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
    HOME=/tmp
# Import is offline and CPU-only. It is an actual build gate only when the
# separately authorized build really runs; source inspection does not pass it.
RUN mkdir /opt/runtime-evidence && chown 1000:1000 /opt/runtime-evidence
USER 1000:1000
WORKDIR /opt/mediacenter
RUN python3.11 -c "import _ssl,_hashlib,zlib,bz2,lzma,ctypes,sqlite3,readline,_uuid; import torch,torchvision,diffusers,transformers,accelerate,safetensors,sentencepiece,numpy,PIL,google.protobuf,redis; import mediacenter.worker_runtime; import importlib.metadata,json,sys,sysconfig,os; assert os.getuid()==1000; print(json.dumps({'python':sys.version,'packages':sorted([(d.metadata['Name'],d.version) for d in importlib.metadata.distributions()]),'compiler':sysconfig.get_config_var('CC'),'configure':sysconfig.get_config_var('CONFIG_ARGS'),'uid':os.getuid(),'imports':'passed'}))" > /opt/runtime-evidence/python.json \
    && python3.11 -m pip check \
    && dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /opt/runtime-evidence/system.tsv
CMD ["python3.11"]
