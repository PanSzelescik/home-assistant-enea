"""Uruchamiany przez Python podczas startu (przed załadowaniem jakichkolwiek modułów).

Na Windows:
- Podmienia brakujące moduły linuksowe (`fcntl`, `resource`, …) atrapami.
- Zachowuje oryginalną klasę `socket.socket` i podmienia `socket.socketpair`
  na implementację używającą oryginału — dzięki temu asyncio ProactorEventLoop
  może stworzyć wewnętrzny pipe nawet gdy pytest-socket blokuje socket.socket.
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    from unittest.mock import MagicMock

    # Moduły istniejące wyłącznie na Linuksie — wymagane przez homeassistant.runner
    for _mod in ("fcntl", "resource", "grp", "pwd"):
        sys.modules.setdefault(_mod, MagicMock())

    import socket as _sock

    # Zapisz oryginalną klasę socket PRZED tym, jak pytest-socket podmieni ją
    # na _SocketBlocker. Dzięki temu nasza socketpair() zawsze działa.
    _RealSocket = _sock.socket

    def _safe_socketpair(
        family: int = _sock.AF_INET,
        type: int = _sock.SOCK_STREAM,
        proto: int = 0,
    ) -> tuple[_sock.socket, _sock.socket]:
        """socketpair() dla Windows używający oryginalnego socket.socket.

        Python 3.12+ na Windows nie ma natywnego socketpair() — stdlib używa
        fallbacku tworzącego parę przez loopback 127.0.0.1. Zastępujemy ten
        fallback wersją korzystającą z zachowanej oryginalnej klasy socket,
        co omija blokadę pytest-socket (która podmienia socket.socket po
        naszym sitecustomize.py).

        Używamy lsock._accept() zamiast lsock.accept(), bo accept() wewnętrznie
        woła socket.socket(fileno=fd) — co po załadowaniu pytest-socket trafi
        w _SocketBlocker. _accept() zwraca sam fd; opakowujemy go przez _RealSocket.
        """
        lsock = _RealSocket(family, type, proto)
        try:
            lsock.bind(("127.0.0.1", 0))
            lsock.listen(1)
            addr = lsock.getsockname()
            csock = _RealSocket(family, type, proto)
            csock.setblocking(False)
            try:
                csock.connect_ex(addr)
                fd, _ = lsock._accept()  # type: ignore[attr-defined]
                ssock = _RealSocket(family, type, proto, fileno=fd)
            except Exception:
                csock.close()
                raise
        finally:
            lsock.close()
        csock.setblocking(True)
        return ssock, csock

    _sock.socketpair = _safe_socketpair  # type: ignore[assignment]
