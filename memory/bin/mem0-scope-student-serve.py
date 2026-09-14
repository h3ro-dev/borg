#!/usr/bin/env python
"""Scope-student mlx_lm server with the adapter-loading fix. mlx_lm 0.31.3's ModelProvider.load() resolves the model
alias before looking the adapter up, so --adapter-path is silently dropped and the BASE model answers (proven again
2026-09-02 05:10 MDT: trust gate 1/20). Same patch as mem0-capture-student-serve, copied deliberately (that file is a live
service). Usage: mem0-scope-student-serve.py --model <base> --adapter-path <dir> --port 11470 [mlx_lm server flags]"""
import sys
from mlx_lm import server as mlx_server

def load(self, model_path, adapter_path=None, draft_model_path=None):
    requested = model_path
    resolved = self._model_map.get(model_path, model_path)
    adapter_path = self._adapter_map.get(requested, adapter_path)
    if adapter_path is None:
        cli_adapter = getattr(self.cli_args, "adapter_path", None)
        if cli_adapter and resolved == self.cli_args.model:
            adapter_path = cli_adapter
    draft_model_path = self._draft_model_map.get(draft_model_path, draft_model_path)
    model_key = (resolved, adapter_path, draft_model_path)
    if self.model_key != model_key:
        print(f"[scope-student] loading model={resolved!r} adapter={adapter_path!r}", flush=True)
        self._load(*model_key)
    return self.model, self.tokenizer

mlx_server.ModelProvider.load = load
if __name__ == "__main__":
    sys.argv = [sys.argv[0]] + sys.argv[1:]
    mlx_server.main()
