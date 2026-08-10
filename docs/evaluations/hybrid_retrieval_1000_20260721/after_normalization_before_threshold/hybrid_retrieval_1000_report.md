# Hybrid Retrieval 1000 条评测

- 固定种子: `20260721`
- 测试案例: `1000`
- L2: 500；L3: 500；正向: 800；拒绝边界: 200。
- Redis: `{'url': 'redis://127.0.0.1:6379/15', 'available': True, 'redis_stack_available': False, 'backend': 'redis'}`
- 生产向量器: 本地 64 维 token/trigram 哈希向量。

## 总体结果

| 模式 | Precision@K | 平均 ms | P50 ms | P95 ms | 最大 ms | 1000 条累计 ms | 索引构建 ms |
|---|---:|---:|---:|---:|---:|---:|---:|
| bm25_okapi | 35.9% | 0.159 | 0.150 | 0.281 | 0.660 | 158.8 | 0.380 |
| fuzzy | 8.8% | 30.535 | 29.439 | 49.779 | 62.336 | 30535.2 | 0.000 |
| hybrid_full_scan | 36.2% | 30.535 | 29.439 | 49.779 | 62.336 | 30535.2 | 0.000 |
| keyword | 28.1% | 30.535 | 29.439 | 49.779 | 62.336 | 30535.2 | 0.000 |
| memory_fallback_hybrid | 30.8% | 13.322 | 12.137 | 29.323 | 57.965 | 13321.6 | 0.000 |
| redis_hybrid | 35.9% | 28.971 | 27.517 | 43.461 | 58.951 | 28970.8 | 0.000 |
| vector | 33.6% | 30.535 | 29.439 | 49.779 | 62.336 | 30535.2 | 0.000 |

## L2/L3 分层

| 模式与层级 | Precision@K |
|---|---:|
| bm25_okapi:l2 | 33.4% |
| bm25_okapi:l3 | 38.5% |
| fuzzy:l2 | 17.6% |
| fuzzy:l3 | 0.0% |
| hybrid_full_scan:l2 | 32.4% |
| hybrid_full_scan:l3 | 40.0% |
| keyword:l2 | 20.2% |
| keyword:l3 | 36.0% |
| memory_fallback_hybrid:l2 | 26.7% |
| memory_fallback_hybrid:l3 | 34.8% |
| redis_hybrid:l2 | 32.4% |
| redis_hybrid:l3 | 39.5% |
| vector:l2 | 30.3% |
| vector:l3 | 36.8% |

## 判定说明

- Precision@K：返回结果中属于相关记忆集合的比例。
- 负向案例：无返回且未召回禁止目标才通过，属于严格拒绝指标。
- `redis_hybrid` 是当前生产检索器在 Redis 可用但无 RediSearch 时的真实路径。
- `memory_fallback_hybrid` 是 Redis 不可用时的真实内存路径。
- `hybrid_full_scan` 使用相同启发式分数但扫描全部活动记录，用于定位候选召回损失。
- `bm25_okapi` 使用 `rank_bm25.BM25Okapi`，只统计查询延迟；索引构建耗时单列。

## 边界覆盖

`exact`、`token_reorder`、`synonym_zh`、`paraphrase_en`、`typo`、`noise`、
`partial_drop`、`cross_domain_noise`、`unrelated`、`ambiguous_short`、
`explicit_negation`、`inactive_memory`。
