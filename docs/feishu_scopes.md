# Feishu Bot Scopes

Bot app API permissions. Last updated: 2026-03-07.

## 按模块分类

### Wiki (知识库)
- `wiki:member:create` / `wiki:member:retrieve` / `wiki:member:update`
- `wiki:node:copy` / `wiki:node:create` / `wiki:node:move` / `wiki:node:read` / `wiki:node:retrieve` / `wiki:node:update`
- `wiki:setting:read` / `wiki:setting:write_only`
- `wiki:space:read` / `wiki:space:retrieve` / `wiki:space:write_only`
- `wiki:wiki` / `wiki:wiki:readonly`

### Task (任务)
- `task:attachment:read` / `task:attachment:write`
- `task:comment` / `task:comment:read` / `task:comment:readonly` / `task:comment:write`
- `task:custom_field:read` / `task:custom_field:write`
- `task:section:read` / `task:section:write`
- `task:task` / `task:task:read` / `task:task:readonly` / `task:task:write` / `task:task:writeonly`
- `task:task.event_update_tenant:readonly` / `task:task.privilege:read`
- `task:tasklist.privilege:read` / `task:tasklist:read` / `task:tasklist:write`

### Calendar (日历)
- `calendar:calendar` / `calendar:calendar:create` / `calendar:calendar:read` / `calendar:calendar:readonly` / `calendar:calendar:subscribe` / `calendar:calendar:update` / `calendar:calendar:delete`
- `calendar:calendar.acl:create` / `calendar:calendar.acl:delete` / `calendar:calendar.acl:read`
- `calendar:calendar.event:create` / `calendar:calendar.event:delete` / `calendar:calendar.event:read` / `calendar:calendar.event:reply` / `calendar:calendar.event:update`
- `calendar:calendar.free_busy:read`
- `calendar:exchange.bindings:create` / `calendar:exchange.bindings:delete` / `calendar:exchange.bindings:read`
- `calendar:settings.caldav:create` / `calendar:settings.workhour:read`
- `calendar:time_off:create` / `calendar:time_off:delete` / `calendar:timeoff`

### Docs (文档)
- `docs:doc` / `docs:doc:readonly`
- `docs:document.comment:create` / `docs:document.comment:read` / `docs:document.comment:update` / `docs:document.comment:write_only`
- `docs:document.content:read` / `docs:document.media:download` / `docs:document.media:upload`
- `docs:document.subscription` / `docs:document.subscription:read`
- `docs:document:copy` / `docs:document:export` / `docs:document:import`
- `docs:event.document_deleted:read` / `docs:event.document_edited:read` / `docs:event.document_opened:read` / `docs:event:subscribe`
- `docs:permission.member` / `docs:permission.member:auth` / `docs:permission.member:create` / `docs:permission.member:delete` / `docs:permission.member:readonly` / `docs:permission.member:retrieve` / `docs:permission.member:transfer` / `docs:permission.member:update`
- `docs:permission.setting` / `docs:permission.setting:read` / `docs:permission.setting:readonly` / `docs:permission.setting:write_only`

### Docx (新版文档)
- `docx:document` / `docx:document:create` / `docx:document:readonly` / `docx:document:write_only`
- `docx:document.block:convert`

### Drive (云空间)
- `drive:drive` / `drive:drive:readonly` / `drive:drive:version` / `drive:drive:version:readonly`
- `drive:drive.metadata:readonly` / `drive:drive.search:readonly`
- `drive:export:readonly`
- `drive:file` / `drive:file:download` / `drive:file:readonly` / `drive:file:upload`
- `drive:file.like:readonly` / `drive:file.meta.sec_label.read_only` / `drive:file:view_record:readonly`

### Space (文件夹)
- `space:document.event:read` / `space:document:delete` / `space:document:move` / `space:document:retrieve` / `space:document:shortcut`
- `space:folder:create`

### IM (消息)
- `im:message` / `im:message:readonly` / `im:message:recall` / `im:message:send_as_bot` / `im:message:update`
- `im:message.group_at_msg:readonly` / `im:message.group_msg` / `im:message.p2p_msg:readonly`
- `im:message.pins:read` / `im:message.pins:write_only`
- `im:message.reactions:read` / `im:message.reactions:write_only`
- `im:message.urgent` / `im:message.urgent.status:write` / `im:message.urgent:phone` / `im:message.urgent:sms`
- `im:message:send_multi_depts` / `im:message:send_multi_users` / `im:message:send_sys_msg`
- `im:chat` / `im:chat:create` / `im:chat:delete` / `im:chat:read` / `im:chat:readonly` / `im:chat:update` / `im:chat:operate_as_owner` / `im:chat:moderation:write_only`
- `im:chat.access_event.bot_p2p_chat:read` / `im:chat.announcement:read` / `im:chat.announcement:write_only`
- `im:chat.chat_pins:read` / `im:chat.chat_pins:write_only` / `im:chat.collab_plugins:read` / `im:chat.collab_plugins:write_only`
- `im:chat.managers:write_only` / `im:chat.members:bot_access` / `im:chat.members:read` / `im:chat.members:write_only`
- `im:chat.menu_tree:read` / `im:chat.menu_tree:write_only` / `im:chat.moderation:read`
- `im:chat.tabs:read` / `im:chat.tabs:write_only` / `im:chat.top_notice:write_only`
- `im:chat.widgets:read` / `im:chat.widgets:write_only`
- `im:app_feed_card:write` / `im:biz_entity_tag_relation:read` / `im:biz_entity_tag_relation:write`
- `im:datasync.feed_card.time_sensitive:write`
- `im:resource` / `im:tag:read` / `im:tag:write` / `im:url_preview.update` / `im:user_agent:read`

### Sheets (表格)
- `sheets:spreadsheet` / `sheets:spreadsheet:create` / `sheets:spreadsheet:read` / `sheets:spreadsheet:readonly` / `sheets:spreadsheet:write_only`
- `sheets:spreadsheet.meta:read` / `sheets:spreadsheet.meta:write_only`

### Base / Bitable (多维表格)
- `base:app:copy` / `base:app:create` / `base:app:read` / `base:app:update`
- `base:collaborator:create` / `base:collaborator:delete` / `base:collaborator:read`
- `base:dashboard:copy` / `base:dashboard:read`
- `base:field:create` / `base:field:delete` / `base:field:read` / `base:field:update`
- `base:form:read` / `base:form:update`
- `base:record:create` / `base:record:delete` / `base:record:read` / `base:record:retrieve` / `base:record:update`
- `base:role:create` / `base:role:delete` / `base:role:read` / `base:role:update`
- `base:table:create` / `base:table:delete` / `base:table:read` / `base:table:update`
- `base:view:read` / `base:view:write_only`
- `base:workflow:read` / `base:workflow:write`
- `bitable:app` / `bitable:app:readonly`

### Baike (企业百科)
- `baike:entity` / `baike:entity:exempt_delete` / `baike:entity:exempt_review` / `baike:entity:readonly`

### Slides (幻灯片)
- `slides:presentation:create` / `slides:presentation:read` / `slides:presentation:update` / `slides:presentation:write_only`

### Board (白板)
- `board:whiteboard:node:create` / `board:whiteboard:node:delete` / `board:whiteboard:node:read` / `board:whiteboard:node:update`

### Contact (通讯录)
- `contact:contact.base:readonly` / `contact:user.base:readonly`

### VC (视频会议)
- `vc:alert:readonly` / `vc:export` / `vc:meeting` / `vc:meeting.all_meeting:readonly` / `vc:meeting:readonly`
- `vc:record:readonly` / `vc:report:readonly` / `vc:reserve` / `vc:reserve:readonly`

### Minutes (妙记)
- `minutes:minutes` / `minutes:minutes.basic:read` / `minutes:minutes.statistics:read` / `minutes:minutes.transcript:export` / `minutes:minutes:readonly`
