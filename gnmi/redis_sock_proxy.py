# Expose the host switch's Redis (127.0.0.1:6379 in the shared netns) as the unix socket
# /var/run/redis/redis.sock that sonic-gnmi's swsscommon insists on using.
import asyncio
import os

SOCK = "/var/run/redis/redis.sock"


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def handle(ureader, uwriter):
    treader, twriter = await asyncio.open_connection("127.0.0.1", 6379)
    await asyncio.gather(pipe(ureader, twriter), pipe(treader, uwriter), return_exceptions=True)


async def main():
    os.makedirs(os.path.dirname(SOCK), exist_ok=True)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    server = await asyncio.start_unix_server(handle, path=SOCK)
    os.chmod(SOCK, 0o777)
    async with server:
        await server.serve_forever()


asyncio.run(main())
