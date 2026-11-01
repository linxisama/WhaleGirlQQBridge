"""Select the account connection without changing existing plugin behavior."""


def onebot_transport(runtime):
    if getattr(runtime, 'mode', '') == 'qqbridge':
        return getattr(runtime, 'bridge', None)
    return getattr(runtime, 'onebot', None)
