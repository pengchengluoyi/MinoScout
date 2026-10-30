"""Side-effect admission for an explicit bound Execute, independent of transport."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict
import threading
import uuid
from mino_scout.runtime.capability_epoch import capability_revision

_PROCESS_GENERATION = uuid.uuid4().hex
_FIELDS = ("binding_id", "binding_revision", "operation_ref", "execution_contract_version",
           "node_connection_generation", "scout_process_generation", "capability_revision")

class BindingGuard:
    def __init__(self):
        self.lock = threading.RLock()
        self.process_generation = _PROCESS_GENERATION
        self.connection_generation = ""
        self.revision = ""
        self.executors = []
        self.runtime_capabilities = []

    def set_connection(self, generation):
        with self.lock:
            self.connection_generation = str(generation or "")

    def announce(self, executors, runtime_capabilities):
        with self.lock:
            if executors is not None:
                self.executors = [asdict(row) for row in executors]
            self.runtime_capabilities = deepcopy(runtime_capabilities)
            self.revision = capability_revision(self.executors, self.runtime_capabilities)
            return {"scout_process_generation": self.process_generation,
                    "capability_revision": self.revision, "execution_binding_version": 1}

    def validate(self, req):
        if not any(getattr(req, key, None) for key in _FIELDS):
            return ""  # An old contract remains old; never infer a binding for it.
        if req.execution_contract_version != "execution_binding.v1":
            return "execution_contract_unsupported"
        if (any(not isinstance(getattr(req, key), str) or not getattr(req, key) for key in _FIELDS if key != "binding_revision")
                or type(req.binding_revision) is not int or req.binding_revision != 1):
            return "binding_wire_incomplete"
        with self.lock:
            if not self.connection_generation or req.node_connection_generation != self.connection_generation:
                return "node_connection_generation_changed"
            if req.scout_process_generation != self.process_generation:
                return "scout_process_generation_changed"
            if not self.revision or req.capability_revision != self.revision:
                return "capability_revision_changed"
            order, selected = req.executor_order, req.selected_impl
            if (not isinstance(order, list) or len(order) != 1 or not isinstance(selected, dict)
                    or not selected.get("id") or selected.get("executor") != order[0]
                    or selected.get("runs_on") != "scout" or selected.get("low_level") != req.low_level):
                return "binding_assembly_invalid"
            if any(not isinstance(value, dict) or set(value) != {"version"} or type(value["version"]) is not int or value["version"] != 1
                   for value in (selected.get("params_contract"), selected.get("result_contract"))):
                return "implementation_contract_unsupported"
            manifest = next((row for row in self.executors if row["id"] == order[0]), None)
            if not manifest or manifest.get("available") is not True:
                return "bound_executor_unavailable"
            # Bound plugin dispatch has one named read implementation. Device
            # executors and requires_caps cannot impersonate this host channel.
            if req.capability_id.startswith("plugin.") or order[0] == "host_plugin":
                if (req.capability_id != "plugin.gmail.fetch_otp" or order != ["host_plugin"]
                        or req.low_level != {}):
                    return "binding_plugin_not_supported"
            from mino_scout.platform.web.web_v5 import READS, WRITES, CLEANUP
            strict = req.device_hint.get("execution_mode") == "v5"
            provides = set(manifest.get("provides") or [])
            required = selected.get("requires_caps")
            provided = req.capability_id in provides or (
                isinstance(required, list) and bool(required)
                and all(isinstance(item, str) and item in provides for item in required))
            if strict and order[0] == "playwright":
                provided = req.capability_id in READS | WRITES | CLEANUP
            if not provided:
                return "bound_event_unavailable"
            if req.capability_id == "plugin.gmail.fetch_otp":
                if (selected.get("id") != "program.v1:plugin.gmail.fetch_otp"
                        or req.operation_ref not in {"prep:plugin.gmail.fetch_otp", "do:plugin.gmail.fetch_otp"}
                        or not req.run_id):
                    return "binding_plugin_contract_invalid"
            if req.capability_id == "input_text":
                if type(req.input_contract_version) is not int or req.input_contract_version != 2:
                    return "input_contract_unsupported"
                mode = req.params.get("mode") or "replace"
                target = req.params.get("target_mode") or req.params.get("point_policy") or ""
                if not any(row.get("id") == "input_text" and row.get("executor") == order[0]
                        and row.get("platform") == req.platform and type(row.get("contract_version")) is int and row["contract_version"] == 2
                        and any(combination.get("target_mode") == target and mode in combination.get("write_modes", [])
                                for combination in row.get("supported_combinations", []))
                        for row in self.runtime_capabilities):
                    return "input_combination_unsupported"
        return ""
