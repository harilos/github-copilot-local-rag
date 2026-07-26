# AC_EXACT_001 Quality Review

対象query:

```text
A2Wに関する情報を教えて
```

## 判定

`AC_EXACT_001` は、Exact正常系テストとしては前提誤り。ただし削除せず、偽陽性回帰テストとして残す。

旧挙動では `A2W` が `A2` aliasへ展開され、`A2L` を含むchunkが `exact` signal付きで返っていた。これはExact検索の偽陽性であり、品質上はP0寄りの不具合。

修正後はExact検索からlossyな自動alias経路を外し、DB側の `A2L -> a2` aliasが残っていてもExact lookupでは使わないようにした。

## 旧挙動の問題

観測された旧top1:

```text
lbnl_2017_energy_efficient_low_gwp_room_air_conditioners.pdf
Page 66 #2
signals: exact, neighbor
debug: anchor_rescue=true, cold_lexical_fast_path=true
```

ただし本文に存在するのは `A2W` ではなく `A2L`。

原因:

```text
A2W -> query lookup aliases included a2
A2L -> DB identifier aliases included a2
a2 matched A2L postings
```

これにより、存在しないIDに対してExact hitが成立していた。

## 修正後の確認: `AC_EXACT_NEG_COLLISION_001`

実行コマンド:

```bash
.copilot/rag/query/.venv/bin/python .copilot/rag/query/search.py \
  --db ac-rag \
  --retrieval-mode hybrid \
  --format json \
  --explain \
  --budget-tokens 1200 \
  --max-chars 1200 \
  --timeout 120 \
  --no-daemon \
  "A2Wに関する情報を教えて"
```

結果:

| 観点 | 結果 |
|---|---|
| exit code | 0 |
| stdout JSON parse | PASS |
| model load log | stderrのみ |
| status | `partial` |
| exact candidate count | 0 |
| unmatched identifiers | `A2W` |
| top1 | `sample_global_cooling_temperature_memo.docx` |
| top1 signals | `lexical` |
| top1 contains `A2W` | false |
| top1 contains `A2L` | false |
| Exact anchor rescue | なし |

この結果は「A2WのExactが正しく当たった」ことを意味しない。`A2W` はDBにないため、Exact候補0件と `unmatched_identifiers=["A2W"]` を期待する偽陽性回帰テストとして扱う。

## 正常系の分離: `AC_EXACT_LOWDF_001`

`A2L` はDB内に存在し、Exact候補として使える。

実行コマンド:

```bash
.copilot/rag/query/.venv/bin/python .copilot/rag/query/search.py \
  --db ac-rag \
  --retrieval-mode hybrid \
  --format json \
  --explain \
  --budget-tokens 1200 \
  --max-chars 1200 \
  --timeout 120 \
  --no-daemon \
  "A2Lに関する情報を教えて"
```

結果:

| 観点 | 結果 |
|---|---|
| exit code | 0 |
| stdout JSON parse | PASS |
| status | `ok` |
| exact candidate count | 3 |
| unmatched identifiers | `[]` |
| top1 | `jraia_2025_inverter_refrigerant_ratio.pdf` |
| top1 signals | `exact`, `neighbor` |
| debug | `exact_match.match_kind=casefold_exact`, `matched_terms=["a2l"]`, `anchor_rescue=true` |

ただし、`A2L` は複数文書に出現するため「一意Exact」ではなく `exact-low-df` として分類する。

## 最終評価

| 観点 | 判定 |
|---|---|
| I/F | PASS |
| JSON stdout純度 | PASS after fix |
| 単発速度 | PASS |
| `A2W` 正常系Exact品質 | 対象外 |
| `A2W` 偽陽性回帰 | PASS after fix |
| `A2W` 偽Exact防止 | PASS after fix |
| `A2L` Exact動作 | 条件付きPASS |
| Dense有効性 | 対象外 |

## 反映すべきテスト仕様

- `A2W` のようなDBに存在しないIDは、削除せず偽陽性回帰テストへ移す。
- Exactは完全なidentifier equalityへ限定し、lossyな自動生成aliasを使わない。
- `anchor_rescue` は `raw_exact`、一意なfull-length `casefold_exact`、完全一致filename/pathだけに限定する。
- strong identifierが未一致なら、`unmatched_identifiers` と `exact_candidate_count` を返す。
- Exact品質テストでは、gold identifierが本文またはidentifier辞書に存在することを事前検証する。
- `--explain` にはmatched term、canonical term、match_kind、retriever rank、RRF寄与を出す必要がある。
