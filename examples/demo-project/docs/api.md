# 用户中心 API 说明

本文档描述用户中心对外接口，是自动化测试的主要依据。

## 用户列表

`GET /api/users` 返回用户列表。

- 分页参数 `page`（默认 1）、`size`（默认 20）
- 响应字段：`items`（用户数组）、`total`（总数）
- 响应时间 SLA：接口响应时间应小于 200 ms

## 创建用户

`POST /api/users` 创建用户。

请求体字段：

- `name`（字符串，必填，不能为空）
- `email`（字符串，必填，格式为邮箱）
- `role`（字符串，可选，取值 `admin` 或 `user`，默认 `user`）

成功返回 201，响应字段包含 `id`、`name`、`email`、`role`。

## 用户详情

`GET /api/users/{id}` 按 id 查询用户。

- `id` 为整数，路径参数
- 用户不存在时返回 404，响应字段 `message`
- 需要登录后访问

## 创建订单

`POST /api/orders` 创建订单。

请求体字段：

- `user_id`（整数，必填）
- `amount`（数字，必填，必须大于 0，单位为分）
- `remark`（字符串，可选，最长 200 字）

成功返回 201，响应字段包含 `id`、`amount`、`status`。新订单状态为 `created`。

## 订单详情

`GET /api/orders/{id}` 查询订单详情，响应字段包含 `id`、`amount`、`status`。

## 管理统计

`GET /api/admin/stats` 返回平台统计信息，需要管理员 token，未鉴权时必须返回 401。

## 导出报表

`GET /api/reports/export` 导出日报表（注意：代码与 spec 里都还没有实现这个接口）。
