import os
from pathlib import Path
import pytest
from anzhuo.checkpoints import Checkpoints

def setup(tmp_path):
    root = tmp_path / 'work'; root.mkdir()
    return root, Checkpoints(tmp_path / 'snapshots')

@pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位在 Windows 上无意义")
def test_restore_create_modify_delete_and_undo(tmp_path):
    root, store = setup(tmp_path)
    (root/'中文.bin').write_bytes(b'\x00old\xff')
    (root/'gone').write_text('removed')
    (root/'mode').write_text('executable'); (root/'mode').chmod(0o755)
    before = store.capture([str(root)])
    (root/'中文.bin').write_bytes(b'\x00new\xff')
    (root/'gone').unlink(); (root/'new').write_text('new')
    (root/'mode').chmod(0o644)
    after = store.capture([str(root)])
    undo = store.apply(store.plan([(before, after)]))
    assert (root/'中文.bin').read_bytes() == b'\x00old\xff'
    assert (root/'gone').read_text() == 'removed' and not (root/'new').exists()
    assert (root/'mode').stat().st_mode & 0o777 == 0o755
    store.undo(undo)
    assert (root/'中文.bin').read_bytes() == b'\x00new\xff'
    assert not (root/'gone').exists() and (root/'new').read_text() == 'new'

def test_conflict_keeps_every_file(tmp_path):
    root, store = setup(tmp_path)
    for name in ['a', 'b']: (root/name).write_text('old')
    a = store.capture([str(root)])
    for name in ['a', 'b']: (root/name).write_text('new')
    b = store.capture([str(root)])
    (root/'b').write_text('human')
    with pytest.raises(ValueError, match='后续改动'): store.apply(store.plan([(a,b)]))
    assert (root/'a').read_text() == 'new' and (root/'b').read_text() == 'human'

def test_changes_between_turns_are_protected(tmp_path):
    root, store = setup(tmp_path)
    f=root/'a';f.write_text('before');a=store.capture([str(root)])
    f.write_text('turn1');b=store.capture([str(root)])
    f.write_text('human');c=store.capture([str(root)])
    f.write_text('turn2');d=store.capture([str(root)])
    with pytest.raises(ValueError, match='轮次之间'): store.plan([(a,b),(c,d)])
    assert f.read_text() == 'turn2'

def test_multi_turn_and_undo_conflict(tmp_path):
    root, store = setup(tmp_path)
    f=root/'a';f.write_text('0');a=store.capture([str(root)])
    f.write_text('1');b=store.capture([str(root)])
    f.write_text('2');c=store.capture([str(root)])
    undo=store.apply(store.plan([(a,b),(b,c)]))
    assert f.read_text()=='0'
    f.write_text('human')
    with pytest.raises(ValueError):store.undo(undo)
    assert f.read_text()=='human'

@pytest.mark.skipif(os.name == "nt", reason="创建符号链接需要特权 (WinError 1314)")
def test_symlink_parent_cannot_escape(tmp_path):
    root, store = setup(tmp_path)
    folder=root/'folder';folder.mkdir();(folder/'a').write_text('old')
    a=store.capture([str(root)]);(folder/'a').write_text('new');b=store.capture([str(root)])
    plan=store.plan([(a,b)])
    (folder/'a').unlink();folder.rmdir()
    outside=tmp_path/'outside';outside.mkdir();(outside/'a').write_text('new')
    folder.symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError):store.apply(plan)
    assert (outside/'a').read_text()=='new'

def test_git_dirty_and_ignored_files(tmp_path):
    import subprocess
    root, store = setup(tmp_path)
    subprocess.run(['git','init','-q',str(root)],check=True)
    (root/'.gitignore').write_text('ignored\n')
    (root/'tracked').write_text('dirty baseline');(root/'ignored').write_text('cache')
    subprocess.run(['git','-C',str(root),'add','tracked','.gitignore'],check=True)
    (root/'tracked').write_text('unsaved user edit');a=store.capture([str(root)])
    (root/'tracked').write_text('AI change');b=store.capture([str(root)])
    store.apply(store.plan([(a,b)]))
    assert (root/'tracked').read_text()=='unsaved user edit'
    assert (root/'ignored').read_text()=='cache'

def test_incomplete_snapshot_refuses_restore(tmp_path,monkeypatch):
    from anzhuo import checkpoints
    root, store = setup(tmp_path)
    (root/'large').write_bytes(b'1234')
    monkeypatch.setattr(checkpoints,'MAX_FILE',3)
    a=store.capture([str(root)])
    with pytest.raises(ValueError,match='不完整'):store.plan([(a,a)])

def test_restore_failure_rolls_back_written_files(tmp_path, monkeypatch):
    root, store = setup(tmp_path)
    for name in ['a','b']: (root/name).write_text('before')
    a=store.capture([str(root)])
    for name in ['a','b']: (root/name).write_text('after')
    b=store.capture([str(root)])
    original=store._write
    calls=0
    def fail_second(path,state):
        nonlocal calls
        calls+=1
        if calls==2: raise OSError('模拟写盘失败')
        original(path,state)
    monkeypatch.setattr(store,'_write',fail_second)
    with pytest.raises(OSError):store.apply(store.plan([(a,b)]))
    assert all((root/name).read_text()=='after' for name in ['a','b'])

def test_corrupt_backup_is_rejected_before_writing(tmp_path):
    root, store = setup(tmp_path)
    f=root/'a';f.write_text('before');a=store.capture([str(root)])
    f.write_text('after');b=store.capture([str(root)])
    plan=store.plan([(a,b)])
    obj=store.objects / plan['changes'][str(f)]['after']['hash']
    obj.write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='校验失败'):store.apply(plan)
    assert f.read_text()=='after'
