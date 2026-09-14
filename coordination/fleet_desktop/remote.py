"""Desktop binding over the unchanged parent nonce/epoch RemoteSupervisor transport."""
from fleet_browser.remote import RemoteSupervisor, RemoteError

class RemoteDesktopSupervisor(RemoteSupervisor):
    def __init__(self, **config):
        super().__init__(**config)
        self.generations = {}

    def open(self, lease, options):
        options = dict(options)
        fence = options.pop('fence')
        fence()  # Trusted Hub assignment callback; never serialized.
        self.generations[lease['lease_id']] = lease['generation']
        return super().open(lease, options)

    def call(self, lease_id, operation, args, timeout=30, fence=None):
        if fence is None or lease_id not in self.generations:
            raise RemoteError('desktop_binding_missing')
        fence()
        return self._call('call', dict(lease_id=lease_id, operation=operation, args=args,
                          timeout=timeout, generation=self.generations[lease_id]))


def serve(config_path):
    """Fixed private service configuration; one fence carrier per service life."""
    import signal
    import threading
    from fleet_browser.integration import private_config
    from fleet_browser.remote import SupervisorServer
    from .fencing import DesktopFenceClient
    from .tart import TartSupervisor

    config = private_config(config_path)
    if set(config) != {'socket_path', 'state_dir', 'actuator', 'fence'}:
        raise RemoteError('invalid_config')
    fence = DesktopFenceClient(**config['fence'])
    supervisor = instance = None
    factory = lambda: TartSupervisor(config['state_dir'], dispatch_check=fence, **config['actuator'])
    try:
        supervisor = factory()
        instance = SupervisorServer(config['socket_path'], supervisor, supervisor_factory=factory)
        def stop(*ignored):
            instance.closing.set()
            threading.Thread(target=instance.shutdown, daemon=True).start()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, stop)
        instance.serve_forever(poll_interval=.1)
    finally:
        try:
            if instance is not None:
                instance.server_close()
            elif supervisor is not None:
                supervisor.close()
        finally:
            fence.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    serve(parser.parse_args().config)
