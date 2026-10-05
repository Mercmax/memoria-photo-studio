"""Prevent two web servers from recovering/processing the same persistent queue."""
import os
class InstanceLock:
    def __init__(self,path):self.path=path;self.stream=None
    def __enter__(self):
        self.stream=self.path.open('a+b');self.stream.seek(0)
        if not self.stream.read(1):self.stream.write(b'0');self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.stream.close();raise RuntimeError('Студия уже запущена с этой папкой данных')
        return self
    def __exit__(self,*args):
        if os.name=='nt':
            import msvcrt
            self.stream.seek(0);msvcrt.locking(self.stream.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(self.stream.fileno(),fcntl.LOCK_UN)
        self.stream.close()
