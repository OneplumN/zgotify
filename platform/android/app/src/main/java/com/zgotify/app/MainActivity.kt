package com.zgotify.app

import android.Manifest
import android.app.AlertDialog
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.text.InputType
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject
import java.util.concurrent.Executors
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.DividerItemDecoration
import androidx.recyclerview.widget.RecyclerView
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import com.zgotify.app.databinding.ActivityMessagesBinding

class MainActivity : AppCompatActivity() {
    companion object {
        // Serialize persistence across Activity recreation, including old in-flight requests.
        private val inboxWorker = Executors.newSingleThreadExecutor()
    }
    private lateinit var profiles: ProfileStore
    private lateinit var status: TextView
    private lateinit var progress: ProgressBar
    private lateinit var list: RecyclerView
    private lateinit var adapter: ListMessageAdapter
    private lateinit var swipeRefresh: SwipeRefreshLayout
    private lateinit var emptyInbox: TextView
    private lateinit var cache: RoomMessageCache
    private lateinit var sync: InboxSync
    private var messages = listOf<InboxRow>()
    private var includeHidden = false
    private var loading = false
    private var page = 0L
    @Volatile private var generation = 0L
    @Volatile private var destroyed = false
    private var nextBefore: Long? = null
    private var loginDialog: AlertDialog? = null
    private val accountDialogs = mutableSetOf<AlertDialog>()
    private val worker = inboxWorker
    private val main = Handler(Looper.getMainLooper())
    private val poll = object : Runnable {
        override fun run() {
            if (!loading && profiles.active() != null && page == 0L) refresh()
            main.postDelayed(this, 15000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        profiles = ProfileStore(this)
        cache = RoomMessageCache(applicationContext)
        sync = InboxSync(cache)
        title = "zgotify"
        val binding = ActivityMessagesBinding.inflate(layoutInflater)
        setContentView(binding.root)
        ViewCompat.setOnApplyWindowInsetsListener(binding.root) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        setSupportActionBar(binding.toolbar)
        status = binding.syncStatus
        progress = binding.progress
        list = binding.messagesView
        emptyInbox = binding.emptyInbox
        swipeRefresh = binding.swipeRefresh
        list.layoutManager = LinearLayoutManager(this)
        list.addItemDecoration(DividerItemDecoration(this, DividerItemDecoration.VERTICAL))
        resetList()
        swipeRefresh.setOnChildScrollUpCallback { _, _ -> list.canScrollVertically(-1) }
        swipeRefresh.setOnRefreshListener { refreshLatest(); swipeRefresh.isRefreshing = false }
        updateAccount()
        if (profiles.active() == null) login()
    }

    override fun onResume() {
        super.onResume()
        generation++
        dismissAccountDialogs()
        loading = false
        messages = emptyList()
        resetList()
        status.text = if (profiles.active() == null) "暂无账号" else "验证身份与缓存"
        refresh()
        main.postDelayed(poll, 15000)
    }
    override fun onPause() { main.removeCallbacks(poll); super.onPause() }
    override fun onDestroy() {
        destroyed = true
        generation++
        dismissAccountDialogs()
        main.removeCallbacks(poll)
        worker.execute { cache.close() }
        super.onDestroy()
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menu.add(0, 1, 0, "刷新").setIcon(android.R.drawable.ic_popup_sync).setShowAsAction(MenuItem.SHOW_AS_ACTION_IF_ROOM)
        menu.add(0, 2, 1, "账号")
        menu.add(0, 3, 2, "添加账号")
        menu.add(0, 4, 3, "包含已隐藏消息").isCheckable = true
        menu.add(0, 5, 4, "操作记录")
        menu.add(0, 6, 5, "通知诊断")
        menu.add(0, 7, 6, "更早的消息")
        menu.add(0, 8, 7, "注销当前设备")
        menu.add(0, 9, 8, "修改密码")
        menu.add(0, 10, 9, "待同步修改")
        menu.add(0, 11, 10, "开源许可")
        return true
    }

    override fun onPrepareOptionsMenu(menu: Menu): Boolean {
        listOf(1, 4, 5, 7, 8, 9, 10).forEach { menu.findItem(it)?.isEnabled = !loading }
        menu.findItem(4)?.isChecked = includeHidden
        menu.findItem(7)?.isEnabled = !loading && nextBefore != null
        return super.onPrepareOptionsMenu(menu)
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        when (item.itemId) {
            1 -> refreshLatest()
            2 -> chooseAccount()
            3 -> login()
            4 -> { includeHidden = !includeHidden; item.isChecked = includeHidden; page = 0; refresh() }
            5 -> audit()
            6 -> diagnostics()
            7 -> nextBefore?.let { page = it; refresh() }
            8 -> logout()
            9 -> changePassword()
            10 -> pendingChanges()
            11 -> licenses()
            else -> return super.onOptionsItemSelected(item)
        }
        return true
    }

    private fun updateAccount() {
        generation++
        dismissAccountDialogs()
        loading = false
        includeHidden = false
        invalidateOptionsMenu()
        progress.visibility = View.GONE
        page = 0
        nextBefore = null
        messages = emptyList()
        resetList()
        supportActionBar?.subtitle = profiles.active()?.let { "${it.username} · ${it.server}" } ?: "未登录"
        status.text = if (profiles.active() == null) "暂无账号" else ""
        refresh()
    }

    private fun <T> request(profile: Profile, operation: () -> T, done: (T) -> Unit) {
        if (loading) return
        loading = true
        invalidateOptionsMenu()
        progress.visibility = View.VISIBLE
        val epoch = generation
        worker.execute {
            val result = runCatching {
                if (!current(profile, epoch)) throw StaleSessionException()
                operation()
            }.onFailure { error ->
                if (error is ApiException && error.status == 401 && profile.personId > 0 && current(profile, epoch)) runCatching { cache.clear(profile.cacheKey) }
            }
            main.post {
                if (!current(profile, epoch)) return@post
                loading = false
                invalidateOptionsMenu()
                progress.visibility = View.GONE
                result.onSuccess(done).onFailure { error ->
                    status.setTextColor(Color.rgb(170, 35, 35))
                    status.text = when ((error as? ApiException)?.status) {
                        401 -> "登录已失效，请重新登录"
                        409 -> "状态已在其他设备更新，请刷新后重试"
                        else -> if (error is IllegalStateException) error.message ?: "操作失败" else "请求失败，请稍后重试"
                    }
                    if (error is ApiException && error.status == 401) {
                        dismissAccountDialogs()
                        profiles.save(profile.copy(token = ""))
                        messages = emptyList()
                        resetList()
                        login(profile)
                    }
                }
            }
        }
    }

    private fun current(profile: Profile, epoch: Long): Boolean =
        !destroyed && epoch == generation && profiles.active() == profile

    private fun showAccountDialog(dialog: AlertDialog) {
        accountDialogs.add(dialog)
        dialog.setOnDismissListener { accountDialogs.remove(dialog) }
        dialog.show()
    }

    private fun dismissAccountDialogs() {
        accountDialogs.toList().forEach { it.dismiss() }
        accountDialogs.clear()
    }

    private fun refresh(force: Boolean = false) {
        val profile = profiles.active() ?: return
        if (profile.token.isEmpty() || profile.personId <= 0) { status.text = "需要重新登录"; return }
        val epoch = generation
        val hidden = includeHidden
        val before = page
        request(profile, {
            val cached = sync.cached(profile.cacheKey, hidden, before)
            main.post {
                if (current(profile, epoch)) render(cached.copy(offline = true))
            }
            val remote = GuardedInboxRemote(ApiInboxRemote(profile)) { current(profile, epoch) }
            sync.refresh(profile.cacheKey, profile.personId, remote, hidden, before, force)
        }, ::render)
    }

    internal fun refreshLatest() {
        if (loading) return
        page = 0
        refresh(force = true)
    }

    private fun render(snapshot: InboxSnapshot) {
        messages = snapshot.rows
        nextBefore = snapshot.nextBefore
        invalidateOptionsMenu()
        status.setTextColor(if (snapshot.offline || snapshot.conflictCount > 0) Color.rgb(160, 65, 20) else Color.DKGRAY)
        val connection = when { snapshot.cacheExpired -> "请联网验证身份"; snapshot.offline -> "离线缓存"; else -> "在线" }
        status.text = "$connection · ${messages.size} 条 · 待同步 ${snapshot.pendingCount} · 待处理 ${snapshot.conflictCount}"
        adapter.submitList(messages)
        emptyInbox.visibility = if (messages.isEmpty()) View.VISIBLE else View.GONE
    }

    private fun resetList() {
        // A fresh adapter fences asynchronous diffs from the previous personal account.
        val profile = profiles.active()
        val epoch = generation
        adapter = ListMessageAdapter(this,
            { row -> if (profile != null && current(profile, epoch)) showMessage(row) },
            { row -> if (profile != null && current(profile, epoch)) messageActions(row) },
        )
        list.adapter = adapter
        emptyInbox.visibility = View.GONE
    }

    private fun login(existing: Profile? = null) {
        if (loginDialog?.isShowing == true) return
        val server = field("服务地址", existing?.server ?: "")
        val username = field("个人账号", existing?.username ?: "")
        val password = field("密码", secret = true)
        val form = column(server, username, password)
        val dialog = AlertDialog.Builder(this).setTitle("登录 zgotify").setView(form).setNegativeButton("取消", null).setPositiveButton("登录", null).create()
        loginDialog = dialog
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val endpoint = runCatching { Api.normalizeServer(server.text.toString()) }.getOrElse { server.error = it.message; return@setOnClickListener }
                val candidate = Profile(endpoint, username.text.toString().trim(), "")
                val body = JSONObject().put("username", candidate.username).put("password", password.text.toString()).put("device_name", "${Build.MANUFACTURER} ${Build.MODEL}".take(128))
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = false
                worker.execute {
                    val result = runCatching { Api.login(candidate, body) }
                    main.post {
                        if (isDestroyed) {
                            result.getOrNull()?.let(::revokeUnused)
                            return@post
                        }
                        dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
                        result.onSuccess { authenticated ->
                            if (!dialog.isShowing) {
                                revokeUnused(authenticated)
                                return@onSuccess
                            }
                            runCatching {
                                profiles.save(authenticated)
                            }
                                .onSuccess { password.text.clear(); dialog.dismiss(); updateAccount() }
                                .onFailure { revokeUnused(authenticated); password.error = "无法保存账号，请重试" }
                        }.onFailure { if (dialog.isShowing) password.error = it.message ?: "登录失败" }
                    }
                }
            }
        }
        dialog.show()
    }

    private fun chooseAccount() {
        val all = profiles.all()
        AlertDialog.Builder(this).setTitle("账号")
            .setItems(all.map { "${it.username}\n${it.server}" }.toTypedArray()) { _, index ->
                val selected = all[index]
                profiles.select(selected)
                updateAccount()
                if (selected.token.isEmpty() || selected.personId <= 0) login(selected)
            }
            .setPositiveButton("添加", { _, _ -> login() }).setNegativeButton("取消", null).create().also(::showAccountDialog)
    }

    private fun revokeUnused(profile: Profile) {
        Thread { runCatching { Api.request(profile, "DELETE", "/v1/session") } }.start()
    }

    private fun showMessage(row: InboxRow) {
        if (loading) return
        val profile = profiles.active() ?: return
        val epoch = generation
        val message = row.message
        if (row.pending?.conflict == true || row.pending?.problem != null) { resolveConflict(profile, row); return }
        val body = TextView(this).apply { setPadding(32, 20, 32, 20); setTextIsSelectable(true) }
        MarkwonFactory.createForMessage(this).renderBody(body, message.body, message.contentType)
        val content = android.widget.ScrollView(this).apply { addView(body) }
        AlertDialog.Builder(this).setTitle(message.title).setView(content)
            .setPositiveButton("操作") { _, _ ->
                if (!current(profile, epoch)) return@setPositiveButton
                messageActions(row)
            }.setNegativeButton("关闭", null).create().also(::showAccountDialog)
    }

    private fun messageActions(row: InboxRow) {
        if (loading) return
        val profile = profiles.active() ?: return
        val epoch = generation
        if (row.pending?.conflict == true || row.pending?.problem != null) { resolveConflict(profile, row); return }
        val state = row.message.state
        val options = arrayOf(if (state.read) "标记未读" else "标记已读", if (state.hidden) "取消隐藏" else "为我隐藏", if (state.favorite) "取消收藏" else "收藏")
        AlertDialog.Builder(this).setTitle("个人状态").setItems(options) { _, index ->
            if (!current(profile, epoch)) return@setItems
            val property = StateField.entries[index]
            val value = !listOf(state.read, state.hidden, state.favorite)[index]
            request(profile, {
                sync.queue(profile.cacheKey, row.message.id, property, value)
                sync.cached(profile.cacheKey, includeHidden, page)
            }) { render(it); refresh(force = true) }
        }.setNegativeButton("取消", null).create().also(::showAccountDialog)
    }

    private fun licenses() {
        val text = assets.open("licenses/gotify-MIT.txt").bufferedReader().use { it.readText() }
        val body = TextView(this).apply { this.text = text; setPadding(24, 16, 24, 16); setTextIsSelectable(true) }
        AlertDialog.Builder(this).setTitle("Gotify · MIT")
            .setView(android.widget.ScrollView(this).apply { addView(body) })
            .setPositiveButton("关闭", null).show()
    }

    private fun resolveConflict(profile: Profile, row: InboxRow) {
        val epoch = generation
        request(profile, { cache.message(profile.cacheKey, row.message.id) }) { latest ->
            if (latest == null) { refresh(); return@request }
            fun flags(value: PersonalState) = "${if (value.read) "已读" else "未读"} / ${if (value.hidden) "已隐藏" else "可见"} / ${if (value.favorite) "已收藏" else "未收藏"}"
            fun resolve(local: Boolean) {
                if (!current(profile, epoch)) return
                request(profile, { sync.resolve(profile.cacheKey, row.message.id, local) }) { refresh(force = true) }
            }
            AlertDialog.Builder(this).setTitle("同步待处理")
                .setMessage("${row.pending?.problem ?: "其他设备已更新此消息"}\n\n服务器：${flags(latest.state)}\n本机修改：${flags(row.message.state)}")
                .setPositiveButton("保留本机修改") { _, _ -> resolve(true) }
                .setNegativeButton("使用服务器状态") { _, _ -> resolve(false) }
                .setNeutralButton("取消", null).create().also(::showAccountDialog)
        }
    }

    private fun pendingChanges() {
        val profile = profiles.active() ?: return
        if (profile.personId <= 0) return
        val epoch = generation
        request(profile, { sync.pendingRows(profile.cacheKey) }) { rows ->
            if (rows.isEmpty()) { Toast.makeText(this, "没有可处理的修改，请先联网刷新", Toast.LENGTH_SHORT).show(); return@request }
            AlertDialog.Builder(this).setTitle("待同步修改")
                .setItems(rows.map { it.message.title }.toTypedArray()) { _, index ->
                    if (current(profile, epoch)) showMessage(rows[index])
                }.setNegativeButton("关闭", null).create().also(::showAccountDialog)
        }
    }

    private fun audit() {
        val profile = profiles.active() ?: return
        request(profile, { Api.request(profile, "GET", "/v1/audit") }) { response ->
            val events = response.getJSONArray("events")
            val text = (0 until events.length()).joinToString("\n\n") { index ->
                val event = events.getJSONObject(index)
                "${event.getString("created_at")}\n${event.getString("action")} · 用户 ${event.getLong("actor_id")}\n${event.getString("device_name")} · 对象 ${event.getLong("object_id")}" 
            }
            val view = TextView(this).apply { this.text = text.ifEmpty { "暂无记录" }; setPadding(24, 12, 24, 12) }
            AlertDialog.Builder(this).setTitle("操作记录").setView(android.widget.ScrollView(this).apply { addView(view) }).setPositiveButton("关闭", null).create().also(::showAccountDialog)
        }
    }

    private fun logout() {
        val profile = profiles.active() ?: return
        AlertDialog.Builder(this).setTitle("注销当前设备？").setNegativeButton("取消", null).setPositiveButton("注销") { _, _ ->
            request(profile, {
                if (profile.token.isNotEmpty()) {
                    try { Api.request(profile, "DELETE", "/v1/session") }
                    catch (error: ApiException) { if (error.status != 401) throw error }
                }
                if (profile.personId > 0) cache.clear(profile.cacheKey)
            }) { profiles.remove(profile); updateAccount() }
        }.create().also(::showAccountDialog)
    }

    private fun changePassword() {
        val profile = profiles.active() ?: return
        val old = field("当前密码", secret = true)
        val next = field("新密码", secret = true)
        AlertDialog.Builder(this).setTitle("修改密码").setView(column(old, next)).setNegativeButton("取消", null).setPositiveButton("修改") { _, _ ->
            val body = JSONObject().put("old_password", old.text.toString()).put("new_password", next.text.toString())
            request(profile, {
                Api.request(profile, "POST", "/v1/password", body)
                if (profile.personId > 0) cache.clear(profile.cacheKey)
            }) { profiles.save(profile.copy(token = "")); updateAccount(); login(profile) }
        }.create().also(::showAccountDialog)
    }

    private fun diagnostics() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel("zgotify_alerts_high", "重要通知", NotificationManager.IMPORTANCE_HIGH))
        val channel = manager.getNotificationChannel("zgotify_alerts_high")
        val granted = Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
        val detail = "通知权限：$granted\n应用通知：${manager.areNotificationsEnabled()}\n通道重要性：${channel.importance}\n远程推送：尚未接入\n版本：${BuildConfig.VERSION_NAME}"
        AlertDialog.Builder(this).setTitle("通知诊断").setMessage(detail).setNegativeButton("关闭", null)
            .setNeutralButton("系统设置") { _, _ -> startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, packageName)) }
            .setPositiveButton("测试通知") { _, _ ->
                if (!granted && Build.VERSION.SDK_INT >= 33) { requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1) }
                else {
                    manager.notify(1, Notification.Builder(this, "zgotify_alerts_high").setSmallIcon(R.drawable.ic_alarm)
                        .setContentTitle("zgotify").setContentText("本机测试通知").setAutoCancel(true).build())
                    Toast.makeText(this, "已提交测试通知", Toast.LENGTH_SHORT).show()
                }
            }.show()
    }

    private fun field(hint: String, value: String = "", secret: Boolean = false) = EditText(this).apply {
        this.hint = hint
        setText(value)
        isSingleLine = true
        inputType = if (secret) InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD else InputType.TYPE_CLASS_TEXT
        if (secret) { isSaveEnabled = false; importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO }
    }

    private fun column(vararg views: View) = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(28, 12, 28, 12)
        views.forEach { addView(it, LinearLayout.LayoutParams(-1, -2)) }
    }
}
