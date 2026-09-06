# Krea 2 is an isolated derived layer over the shared verified runtime.
# The release builder supplies the exact offline wheel closure; builds have no network.
FROM runtime_parent:mc044-v1
USER 0:0
COPY locked/krea2/ /offline/krea2-wheels/
COPY locked/krea2.requirements.txt /offline/krea2.requirements.txt
RUN /opt/python/bin/python3.11 -m venv --system-site-packages /opt/krea-runtime && \
    /opt/krea-runtime/bin/python -m pip install --no-index --no-deps --require-hashes \
      --find-links=/offline/krea2-wheels -r /offline/krea2.requirements.txt
COPY --chown=1000:1000 sdk/ /opt/mediacenter/
USER 1000:1000
WORKDIR /opt/mediacenter
RUN /opt/krea-runtime/bin/python -m pip check && /opt/krea-runtime/bin/python -B -c \
    "import diffusers,transformers; from diffusers import Krea2Pipeline; import mediacenter.image_worker_cli,mediacenter.adapters.krea2; assert diffusers.__version__=='0.40.0'; assert transformers.__version__=='5.16.1'"
ENTRYPOINT ["/opt/krea-runtime/bin/python","-B","-u","-m","mediacenter.image_worker_cli"]
CMD []
