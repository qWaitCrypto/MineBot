from pathlib import Path
root = Path('/home/qwait/MineBot-always-on')
source = (root / 'logs/step7-life-live-control.py').read_text()
source = source.replace('step7-current-c', 'step7-fixed-g')
source = source.replace("metadata['server_pid']=json.loads((root/'logs/step7-current-b-server-owner.json').read_text())['pid']", "assert metadata['server_pid'] > 0")
source = source.replace('请把背包中已有的原木合成至少4块木板。只用现有材料，不砍树、不破坏或放置方块；完成后告诉我实际背包结果，然后回到空闲生活。',
    '请安全走到住处坐标151.5,70,163.5附近。不要破坏或放置任何方块，不要采集，不要传送，不要外部补给；完成后如实汇报实际位置，再回到空闲生活。')
exec(compile(source, __file__, 'exec'))
