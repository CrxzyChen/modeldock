from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mediacenter.gpu_scheduler import GPUBusyError,GPULeaseScheduler
from mediacenter.instance_policy import InstancePolicy
from mediacenter.repository import Repository
from mediacenter.hardware import HardwareProbe
from tests.test_gpu_reservations import Inventory
from tests.test_resident_policy import seed_deployment,policy_value


class GPUSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.repo=Repository(self.root/'state.db');self.authority=InstancePolicy(self.repo)
        self.policy=policy_value(seed_deployment(self.repo),('GPU-one',))
        self.inventory=Inventory()
        self.memory=SimpleNamespace(available_bytes=64*1024**3,pressure_avg60=0)
        self.scheduler=GPULeaseScheduler((0,),authority=self.authority,gpu_uuids=('GPU-one',),
            hardware=self.inventory,memory_provider=lambda:self.memory)
    def tearDown(self):self.temp.cleanup()
    def test_outside_uuid_pool_rejected_without_observation(self):
        self.policy['gpus']=['GPU-other']
        with self.assertRaisesRegex(GPUBusyError,'outside_explicit_pool'):self.scheduler.capacity(self.policy)
    def test_low_ram_and_pressure_fail_closed(self):
        for available,pressure in ((1,0),(64*1024**3,6),(64*1024**3,float('inf'))):
            self.memory.available_bytes,self.memory.pressure_avg60=available,pressure
            with self.assertRaisesRegex(GPUBusyError,'host_memory_pressure'):self.scheduler.capacity(self.policy)
    def test_telemetry_unknown_or_impossible_fails_closed(self):
        for snapshot in ({'available':False,'items':[]},{'available':True,'items':[]},
                         {'available':True,'items':[{'uuid':'GPU-one','memory_used_mib':-1,'memory_total_mib':49140}]}):
            self.inventory.gpu_snapshot=lambda **_:snapshot
            with self.assertRaises(GPUBusyError):self.scheduler.capacity(self.policy)
    def test_external_process_name_does_not_grant_ownership(self):
        self.inventory.used=3000
        original=self.inventory.gpu_snapshot
        def observed(**kwargs):
            result=original(**kwargs)
            result['items'][0]['processes']=[{'pid':123,'name':'vllm','memory_mib':3000}]
            return result
        self.inventory.gpu_snapshot=observed
        self.scheduler.capacity(self.policy)
        self.assertEqual(self.scheduler.resource_snapshot()['gpus'][0]['processes'][0]['ownership'],'unattributed')
        self.policy['sharing_mode']='exclusive'
        with self.assertRaisesRegex(GPUBusyError,'exclusive_observed'):self.scheduler.capacity(self.policy)
    def test_capacity_snapshot_uses_explicit_short_budget(self):
        calls=[]
        original=self.inventory.gpu_snapshot
        self.inventory.gpu_snapshot=lambda **kw:(calls.append(kw) or original(**kw))
        self.scheduler.capacity(self.policy)
        self.assertEqual(calls,[{'timeout_seconds':.5}])
    def test_durable_base_and_task_budget_must_fit_total_gpu_memory(self):
        self.policy.update(base_mib=6000,task_mib=1000,external_reserve_mib=0)
        self.assertTrue(self.scheduler.validate_budget(self.policy))
        self.policy['base_mib']=7000
        with self.assertRaisesRegex(GPUBusyError,'gpu_policy_unschedulable'):
            self.scheduler.validate_budget(self.policy)
    def test_old_process_context_lease_is_retired(self):
        self.assertFalse(hasattr(self.scheduler,'lease'))
        self.assertFalse(hasattr(self.scheduler,'acquire'))
    def test_missing_telemetry_snapshot_is_unavailable(self):
        self.inventory.gpu_snapshot=lambda **_:{'available':False,'items':[]}
        result=self.scheduler.resource_snapshot()
        self.assertFalse(result['telemetry_available'])
        self.assertFalse(result['gpus'][0]['available_for_new_task'])

if __name__=='__main__':unittest.main()
