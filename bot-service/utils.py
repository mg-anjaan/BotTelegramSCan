# utils.py - helper functions
import hashlib
import os
import aiofiles

async def save_temp_file(file_bytes, suffix=".tmp"):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    async with aiofiles.open(path, "wb") as f:
        await f.write(file_bytes)
    return path

def md5_bytes(b: bytes):
    return hashlib.md5(b).hexdigest()
