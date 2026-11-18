"""持续收件循环：按文件夹代际与 UID 增量，重建后重新枚举，中断可续。"""
from __future__ import annotations

import logging
import time

from .engine import Engine
from .gateway import Gateway, GenerationConflict
from .storage import Store

log = logging.getLogger('collector')


class Collector:
    def __init__(self, gateway: Gateway, store: Store,
                 max_generation_retries: int = 5):
        self.gateway = gateway
        self.store = store
        self.engine = Engine(store)
        self.max_retries = max_generation_retries

    def collect_once(self) -> dict:
        """跑完整一圈：更新通讯录，再逐文件夹增量收件。返回统计。"""
        stats = {'messages_new': 0, 'messages_seen': 0, 'ingest': [],
                 'folders': [], 'conflicts': 0}
        self.store.replace_contacts(self.gateway.contacts())
        for folder in self.gateway.folders():
            stats['folders'].append(folder.name)
            sub = self._collect_folder(folder.name, folder.generation)
            stats['messages_new'] += sub['messages_new']
            stats['messages_seen'] += sub['messages_seen']
            stats['conflicts'] += sub['conflicts']
            stats['ingest'].extend(sub['ingest'])
        self.store.commit()
        return stats

    def _collect_folder(self, name: str, generation: int) -> dict:
        stats = {'messages_new': 0, 'messages_seen': 0,
                 'ingest': [], 'conflicts': 0}
        checkpoint = self.store.checkpoint(name)
        if checkpoint is not None and checkpoint['generation'] != generation:
            # 文件夹已重建：旧序号不再代表同一覆盖，整代重新枚举
            log.info('%s 代际 %s -> %s，重新枚举', name,
                     checkpoint['generation'], generation)
            after = 0
        elif checkpoint is not None:
            after = checkpoint['through']
        else:
            after = 0

        retries = 0
        while True:
            try:
                page = self.gateway.mailbox(name, generation, after)
            except GenerationConflict as conflict:
                retries += 1
                stats['conflicts'] += 1
                if retries > self.max_retries:
                    raise
                log.warning('%s 枚举中遇到代际冲突，按代际 %s 重来',
                            name, conflict.current_generation)
                generation = conflict.current_generation
                after = 0  # 重建窗口里新增的信也要收到，整代重来
                continue
            for ref in page.messages:
                raw = self.gateway.raw(ref.raw_path)
                result = self.engine.ingest(raw)
                # 观察记录与字节解耦：移动前后是同一原信也各留一条
                self.store.add_observation(
                    result.raw_sha256, ref.folder, generation, ref.uid)
                if result.reused:
                    stats['messages_seen'] += 1
                else:
                    stats['messages_new'] += 1
                    stats['ingest'].append({
                        'sha256': result.raw_sha256,
                        'disclosures': result.disclosures,
                        'invalid_disclosures': result.invalid_disclosures,
                        'corrections': result.corrections,
                    })
                # 每封处理完即落检查点；先入账再推进，中断不丢不重
                self.store.save_checkpoint(name, generation, ref.uid)
                self.store.commit()
            self.store.save_checkpoint(name, generation, page.through)
            self.store.commit()
            return stats

    def run_forever(self, interval: float) -> None:
        while True:
            stats = self.collect_once()
            log.info('收件一圈完成：新信 %s 封，已见 %s 封，代际冲突 %s 次',
                     stats['messages_new'], stats['messages_seen'],
                     stats['conflicts'])
            time.sleep(interval)
