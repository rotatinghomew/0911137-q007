# 记录样式

以下为当前业务文件中的实际记录，字段口径见 `data-contract.md`；字段标识保持业务接口写法。

### `visits`

字段：`choice`、`customer`、`day`、`store`、`visit`。

```json
{
  "visit": "v000",
  "customer": "c0",
  "store": "乙店",
  "day": 1,
  "choice": null
}
```

### `offers`

字段：`available`、`item`、`price`、`shown_at`、`visit`。

```json
{
  "visit": "v000",
  "item": "i5",
  "price": 25,
  "available": false,
  "shown_at": 1
}
```

### `items`

字段：`family`、`item`、`launched_at`、`size`。

```json
{
  "item": "i1",
  "family": "常规",
  "size": 2,
  "launched_at": 1
}
```
