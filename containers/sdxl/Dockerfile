FROM runtime_parent
USER 0:0
COPY wheels/ /offline/sdxl-wheels/
COPY requirements.txt /offline/sdxl-requirements.txt
RUN python3.11 -m pip install --no-index --no-deps --require-hashes --find-links=/offline/sdxl-wheels -r /offline/sdxl-requirements.txt
COPY --chown=1000:1000 sdk/ /opt/mediacenter/
USER 1000:1000
WORKDIR /opt/mediacenter
RUN python3.11 -m pip check && python3.11 -c "import os,sys,json,importlib.metadata; import torch,diffusers,transformers,peft,redis; import mediacenter.worker_cli,mediacenter.adapters.sdxl; assert os.getuid()==1000; assert not any(m in sys.modules for m in ('mediacenter.server','mediacenter.repository','mediacenter.config','mediacenter.runtime_provisioning')); print(json.dumps({'python':sys.version,'packages':sorted([(d.metadata['Name'],d.version) for d in importlib.metadata.distributions()]),'uid':os.getuid(),'imports':'passed'}))" > /opt/runtime-evidence/python.json
ENTRYPOINT ["/opt/python/bin/python3.11","-B","-u","-m","mediacenter.worker_cli"]
CMD []
