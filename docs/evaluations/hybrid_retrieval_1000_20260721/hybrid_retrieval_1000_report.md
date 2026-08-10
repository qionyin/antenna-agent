# Hybrid Retrieval 1000 条评测

- 固定种子: `20260721`
- 测试案例: `1000`
- L2: 500；L3: 500；正向: 800；拒绝边界: 200。
- Redis: `{'url': 'redis://127.0.0.1:6379/15', 'available': True, 'redis_stack_available': False, 'backend': 'redis'}`
- 生产向量器: 本地 64 维 token/trigram 哈希向量。

## 总体结果

| 模式 | Precision@K | Recall@K | F1@K | Hit@K | 平均 ms | P50 ms | P95 ms | 最大 ms | 1000 条累计 ms | 索引构建 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| bm25_okapi | 35.9% | 56.7% | 44.0% | 73.0% | 0.125 | 0.121 | 0.185 | 0.394 | 124.7 | 0.380 |
| fuzzy | 8.8% | 4.7% | 6.1% | 11.2% | 19.082 | 20.684 | 24.447 | 33.468 | 19081.8 | 0.000 |
| hybrid_full_scan | 36.2% | 57.0% | 44.3% | 89.1% | 19.082 | 20.684 | 24.447 | 33.468 | 19081.8 | 0.000 |
| keyword | 28.1% | 43.9% | 34.3% | 73.1% | 19.082 | 20.684 | 24.447 | 33.468 | 19081.8 | 0.000 |
| memory_fallback_hybrid | 30.8% | 48.4% | 37.6% | 75.0% | 9.217 | 9.662 | 18.593 | 26.698 | 9217.4 | 0.000 |
| redis_hybrid | 36.1% | 56.3% | 44.0% | 88.9% | 24.061 | 22.951 | 33.624 | 45.778 | 24061.5 | 0.000 |
| vector | 33.6% | 52.8% | 41.0% | 86.5% | 19.082 | 20.684 | 24.447 | 33.468 | 19081.8 | 0.000 |

## L2/L3 分层

| 模式与层级 | Precision@K | Recall@K | F1@K | Hit@K |
|---|---:|---:|---:|---:|
| bm25_okapi:l2 | 33.4% | 55.6% | 41.7% | 71.2% |
| bm25_okapi:l3 | 38.5% | 57.8% | 46.2% | 74.8% |
| fuzzy:l2 | 17.6% | 9.4% | 12.3% | 22.5% |
| fuzzy:l3 | 0.0% | 0.0% | 0.0% | 0.0% |
| hybrid_full_scan:l2 | 32.4% | 53.9% | 40.5% | 91.5% |
| hybrid_full_scan:l3 | 40.0% | 60.0% | 48.0% | 86.8% |
| keyword:l2 | 20.2% | 33.8% | 25.3% | 71.2% |
| keyword:l3 | 36.0% | 54.0% | 43.2% | 75.0% |
| memory_fallback_hybrid:l2 | 26.7% | 44.5% | 33.4% | 75.0% |
| memory_fallback_hybrid:l3 | 34.8% | 52.2% | 41.8% | 75.0% |
| redis_hybrid:l2 | 32.6% | 53.5% | 40.5% | 91.2% |
| redis_hybrid:l3 | 39.7% | 59.1% | 47.5% | 86.5% |
| vector:l2 | 30.3% | 50.4% | 37.9% | 88.5% |
| vector:l3 | 36.8% | 55.1% | 44.1% | 84.5% |

## 判定说明

- Precision@K：返回结果中属于相关记忆集合的比例。
- F1@K：宏平均 Precision@K 与 Recall@K 的调和平均，用作阈值选择主指标。
- 负向案例：无返回且未召回禁止目标才通过，属于严格拒绝指标。
- `redis_hybrid` 是当前生产检索器在 Redis 可用但无 RediSearch 时的真实路径。
- `memory_fallback_hybrid` 是 Redis 不可用时的真实内存路径。
- `hybrid_full_scan` 使用相同启发式分数但扫描全部活动记录，用于定位候选召回损失。
- `bm25_okapi` 使用 `rank_bm25.BM25Okapi`，只统计查询延迟；索引构建耗时单列。

## 边界覆盖

`exact`、`token_reorder`、`synonym_zh`、`paraphrase_en`、`typo`、`noise`、
`partial_drop`、`cross_domain_noise`、`unrelated`、`ambiguous_short`、
`explicit_negation`、`inactive_memory`。
