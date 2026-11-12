package com.zgotify.app

import android.Manifest
import android.app.Activity
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
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

class MainActivity : Activity() {
    private lateinit var profiles: ProfileStore
    private lateinit var status: TextView
    private lateinit var progress: ProgressBar
    private lateinit var list: ListView
    private var messages = listOf<JSONObject>()
    private var includeHidden = false
    private var loading = false
    private var page = 0L
    private var generation = 0L
    private var loginDialog: AlertDialog? = null
    private val worker = Executors.newSingleThreadExecutor()
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
        title = "zgotify"
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(16, 8, 16, 8)
            setOnApplyWindowInsetsListener { view, insets ->
                if (Build.VERSION.SDK_INT >= 30) {
                    val bars = insets.getInsets(android.view.WindowInsets.Type.systemBars())
                    view.setPadding(16, bars.top, 16, bars.bottom)
                } else {
                    view.setPadding(16, insets.systemWindowInsetTop, 16, insets.systemWindowInsetBottom)
                }
                insets
            }
        }
        status = TextView(this).apply { textSize = 14f; setPadding(12, 12, 12, 12) }
        progress = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply { isIndeterminate = true; visibility = View.GONE }
        list = ListView(this)
        root.addView(status)
        root.addView(progress, LinearLayout.LayoutParams(-1, 8))
        root.addView(list, LinearLayout.LayoutParams(-1, 0, 1f))
        setContentView(root)
        list.setOnItemClickListener { _, _, position, _ -> messages.getOrNull(position)?.let(::showMessage) }
        updateAccount()
        if (profiles.active() == null) login()
    }

    override fun onResume() { super.onResume(); main.post(poll) }
    override fun onPause() { main.removeCallbacks(poll); super.onPause() }
    override fun onDestroy() { generation++; main.removeCallbacks(poll); worker.shutdownNow(); super.onDestroy() }

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
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        when (item.itemId) {
            1 -> { page = 0; refresh() }
            2 -> chooseAccount()
            3 -> login()
            4 -> { includeHidden = !includeHidden; item.isChecked = includeHidden; page = 0; refresh() }
            5 -> audit()
            6 -> diagnostics()
            7 -> messages.lastOrNull()?.let { page = it.getLong("id"); refresh() }
            8 -> logout()
            9 -> changePassword()
            else -> return super.onOptionsItemSelected(item)
        }
        return true
    }

    private fun updateAccount() {
        generation++
        loading = false
        progress.visibility = View.GONE
        page = 0
        messages = emptyList()
        list.adapter = ArrayAdapter<String>(this, android.R.layout.simple_list_item_1, emptyList())
        actionBar?.subtitle = profiles.active()?.let { "${it.username} · ${it.server}" } ?: "未登录"
        status.text = if (profiles.active() == null) "暂无账号" else ""
        refresh()
    }

    private fun <T> request(profile: Profile, operation: () -> T, done: (T) -> Unit) {
        if (loading) return
        loading = true
        progress.visibility = View.VISIBLE
        val epoch = generation
        worker.execute {
            val result = runCatching(operation)
            main.post {
                if (isDestroyed || epoch != generation || profiles.active()?.key != profile.key) return@post
                loading = false
                progress.visibility = View.GONE
                result.onSuccess(done).onFailure { error ->
                    status.setTextColor(Color.rgb(170, 35, 35))
                    status.text = when ((error as? ApiException)?.status) {
                        401 -> "登录已失效，请重新登录"
                        409 -> "状态已在其他设备更新，请刷新后重试"
                        else -> error.message ?: "请求失败"
                    }
                    if (error is ApiException && error.status == 401) {
                        profiles.save(profile.copy(token = ""))
                        login(profile)
                    }
                }
            }
        }
    }

    private fun refresh() {
        val profile = profiles.active() ?: return
        if (profile.token.isEmpty()) { status.text = "需要重新登录"; return }
        request(profile, { Api.request(profile, "GET", "/v1/messages?include_hidden=$includeHidden&before=$page") }) { response ->
            val array = response.getJSONArray("messages")
            messages = (0 until array.length()).map { array.getJSONObject(it) }
            status.setTextColor(Color.DKGRAY)
            status.text = if (messages.isEmpty()) "暂无消息" else "${messages.size} 条消息"
            val rows = messages.map { msg ->
                val state = msg.getJSONObject("state")
                val flags = listOfNotNull(if (state.getBoolean("read")) "已读" else "未读", if (state.getBoolean("hidden")) "已隐藏" else null, if (state.getBoolean("favorite")) "已收藏" else null)
                "${msg.getString("title")}\n${flags.joinToString(" · ")} · P${msg.getInt("priority")}\n${msg.getString("message").take(160)}"
            }
            list.adapter = object : ArrayAdapter<String>(this, android.R.layout.simple_list_item_1, rows) {
                override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
                    return (super.getView(position, convertView, parent) as TextView).apply {
                        textSize = 16f
                        setPadding(14, 20, 14, 20)
                        maxLines = 6
                    }
                }
            }
        }
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
                    val result = runCatching { Api.request(candidate, "POST", "/v1/login", body) }
                    main.post {
                        if (isDestroyed) return@post
                        dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
                        result.onSuccess { response ->
                            if (!dialog.isShowing) {
                                worker.execute { runCatching { Api.request(candidate.copy(token = response.getString("token")), "DELETE", "/v1/session") } }
                                return@onSuccess
                            }
                            runCatching { profiles.save(candidate.copy(username = response.getJSONObject("person").getString("username"), token = response.getString("token"))) }
                                .onSuccess { password.text.clear(); dialog.dismiss(); updateAccount() }
                                .onFailure { password.error = "无法保存账号，请重试" }
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
            .setItems(all.map { "${it.username}\n${it.server}" }.toTypedArray()) { _, index -> profiles.select(all[index]); updateAccount() }
            .setPositiveButton("添加", { _, _ -> login() }).setNegativeButton("取消", null).show()
    }

    private fun showMessage(message: JSONObject) {
        val profile = profiles.active() ?: return
        val state = message.getJSONObject("state")
        val options = arrayOf(if (state.getBoolean("read")) "标记未读" else "标记已读", if (state.getBoolean("hidden")) "取消隐藏" else "为我隐藏", if (state.getBoolean("favorite")) "取消收藏" else "收藏")
        val body = TextView(this).apply { text = message.getString("message"); setPadding(32, 20, 32, 20); setTextIsSelectable(true) }
        val content = android.widget.ScrollView(this).apply { addView(body) }
        AlertDialog.Builder(this).setTitle(message.getString("title")).setView(content)
            .setPositiveButton("操作") { _, _ ->
                AlertDialog.Builder(this).setTitle("个人状态").setItems(options) { _, index ->
                    if (profiles.active()?.key != profile.key) return@setItems
                    val property = arrayOf("read", "hidden", "favorite")[index]
                    val patch = JSONObject().put(property, !state.getBoolean(property)).put("expected_version", state.getLong("version"))
                    request(profile, { Api.request(profile, "PATCH", "/v1/messages/${message.getLong("id")}/state", patch) }) { refresh() }
                }.setNegativeButton("取消", null).show()
            }.setNegativeButton("关闭", null).show()
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
            AlertDialog.Builder(this).setTitle("操作记录").setView(android.widget.ScrollView(this).apply { addView(view) }).setPositiveButton("关闭", null).show()
        }
    }

    private fun logout() {
        val profile = profiles.active() ?: return
        AlertDialog.Builder(this).setTitle("注销当前设备？").setNegativeButton("取消", null).setPositiveButton("注销") { _, _ ->
            if (profile.token.isEmpty()) { profiles.remove(profile); updateAccount() }
            else request(profile, {
                try { Api.request(profile, "DELETE", "/v1/session") }
                catch (error: ApiException) { if (error.status != 401) throw error; JSONObject() }
            }) { profiles.remove(profile); updateAccount() }
        }.show()
    }

    private fun changePassword() {
        val profile = profiles.active() ?: return
        val old = field("当前密码", secret = true)
        val next = field("新密码", secret = true)
        AlertDialog.Builder(this).setTitle("修改密码").setView(column(old, next)).setNegativeButton("取消", null).setPositiveButton("修改") { _, _ ->
            val body = JSONObject().put("old_password", old.text.toString()).put("new_password", next.text.toString())
            request(profile, { Api.request(profile, "POST", "/v1/password", body) }) { profiles.save(profile.copy(token = "")); login(profile) }
        }.show()
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
                    manager.notify(1, Notification.Builder(this, "zgotify_alerts_high").setSmallIcon(android.R.drawable.ic_dialog_email)
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
