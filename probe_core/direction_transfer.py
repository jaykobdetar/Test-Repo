"""Fixed public direction transfer evidence; no model or provider operations."""
from pathlib import Path
import stat
import os

from .artifact_store import ArtifactStore
from .audit import canonical_json
from .dispatcher import Dispatcher, TransportError


# Existing prepared public F32 e_0 vector, width 2048, never fitted to data.
DIRECTION_SHA256 = 'sha256:3a8c413d0c6d097b35489c6a17d110116eb49135b671fc3109927238fced4a44'
DIRECTION_BYTES = 8288
DIRECTION_NAME = 'public_basis_direction'


def direction_reference():
    return {'path': DIRECTION_SHA256.removeprefix('sha256:') + '/tensor.safetensors',
            'sha256': DIRECTION_SHA256, 'tensor_name': DIRECTION_NAME}


def expected_upload_receipt():
    return {'path': direction_reference()['path'], 'sha256': DIRECTION_SHA256,
            'tensors': [{'tensor_name': DIRECTION_NAME, 'shape': [2048], 'dtype': 'F32'}]}


def expected_present_readback():
    return {'state': 'present', 'sha256': DIRECTION_SHA256, 'bytes': DIRECTION_BYTES}


def _same(value, expected):
    # JSON equality also distinguishes false/0 and float/integer fields.
    return canonical_json(value) == canonical_json(expected)


def controller_direction(root):
    """Read an existing trusted registration; never create or import one here."""
    root = Path(root)
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or any(parent.is_symlink() for parent in root.parents)):
        raise ValueError('direction registry must be private and trusted-owned')
    record, body = ArtifactStore(root).read(DIRECTION_SHA256.removeprefix('sha256:'), max_bytes=DIRECTION_BYTES)
    expected = {'artifact_id': DIRECTION_SHA256.removeprefix('sha256:'), 'path': direction_reference()['path'],
                'bytes': DIRECTION_BYTES, 'sha256': DIRECTION_SHA256, 'tensor_refs': [direction_reference()]}
    if not _same(record, expected) or len(body) != DIRECTION_BYTES:
        raise ValueError('fixed public direction registration differs')
    return expected


# Runs through the already verified SSH endpoint using the image's existing
# interpreter. Arguments are fixed by the caller; stdout has only public hashes.
# Tests execute this same read-only code on a small temporary directory.
READBACK_PROGRAM = '''import hashlib,itertools,json,os,stat,sys
root,digest,uid,phase=sys.argv[1],sys.argv[2],int(sys.argv[3]),sys.argv[4]
assert len(digest)==64 and all(c in "0123456789abcdef" for c in digest)
assert phase in ("before","after")
def directory(path, parent=None):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
    info=os.fstat(fd)
    assert info.st_uid==uid and not info.st_mode&0o077
    return fd
def names(fd):
    with os.scandir(fd) as entries:
        return [item.name for item in itertools.islice(entries,2)]
top=directory(root)
try:
    if phase=="before":
        assert names(top)==[]
        result={"state":"absent"}
    else:
        assert names(top)==[digest]
        folder=directory(digest,top)
        try:
            assert names(folder)==["tensor.safetensors"]
            fd=os.open("tensor.safetensors",os.O_RDONLY|os.O_NOFOLLOW,dir_fd=folder)
            with os.fdopen(fd,"rb") as stream:
                info=os.fstat(stream.fileno())
                assert stat.S_ISREG(info.st_mode) and info.st_uid==uid and info.st_nlink==1
                assert stat.S_IMODE(info.st_mode)==0o400 and info.st_size==8288
                body=stream.read(8289)
                assert len(body)==8288
                result={"state":"present","sha256":"sha256:"+hashlib.sha256(body).hexdigest(),"bytes":len(body)}
        finally:
            os.close(folder)
    print(json.dumps(result,separators=(",",":")))
finally:
    os.close(top)
'''


class DirectionDispatcher(Dispatcher):
    """The normal staging/POST path, with a required bounded evidence gate."""
    def __init__(self, *args, readback, publish, **kwargs):
        super().__init__(*args, **kwargs)
        self.readback, self.publish = readback, publish

    def _stage_inputs(self, job):
        try:
            if job.spec.operation.kind != 'steer' or job.spec.operation.direction.model_dump(mode='json') != direction_reference():
                raise ValueError('fixed direction required')
            controller = controller_direction(self.input_artifact_root)
            before = self.readback('before')
            if not _same(before, {'state': 'absent'}):
                raise ValueError('remote input store was not empty')
            uploaded = super()._stage_inputs(job)
            if not _same(uploaded, {DIRECTION_SHA256: expected_upload_receipt()}):
                raise ValueError('worker upload receipt differs from the fixed input')
            after = self.readback('after')
            if not _same(after, expected_present_readback()):
                raise ValueError('worker input bytes differ after upload')
            self.publish({'schema_version': 1, 'kind': 'public_direction_transfer',
                'request': self._request(job).model_dump(mode='json'), 'controller': controller,
                'worker_before': before, 'upload_receipt': uploaded[DIRECTION_SHA256], 'worker_after': after,
                'scientific_evidence': False})
            return uploaded
        except (OSError, ValueError, KeyError, TypeError):
            raise TransportError('fixed public direction staging evidence is unavailable or invalid') from None


def verify_direction_evidence(ledger, job, evidence, root, receipt, manifest):
    """Recheck retained input bytes and the exact accepted execution binding."""
    request = Dispatcher(ledger, None, worker_id=job.worker_id, transfer_directory=Path(root))._request(job)
    expected = {'schema_version': 1, 'kind': 'public_direction_transfer',
        'request': request.model_dump(mode='json'), 'controller': controller_direction(root),
        'worker_before': {'state': 'absent'}, 'upload_receipt': expected_upload_receipt(),
        'worker_after': expected_present_readback(), 'scientific_evidence': False}
    if (not _same(evidence, expected) or receipt is None or receipt.manifest != manifest
            or job.spec.operation.kind != 'steer'
            or job.spec.operation.direction.model_dump(mode='json') != direction_reference()
            or manifest.experiment.tool != 'steer_direction'
            or manifest.experiment.intervention_hash != ledger.operation_hash(job.spec)
            or 'tensors.safetensors' not in {item.path for item in manifest.artifacts}):
        raise ValueError('fixed direction transfer evidence differs from accepted execution')
    return evidence
