package com.zgotify.app

import android.content.Context
import android.content.Intent
import android.text.Spanned
import android.text.style.URLSpan
import android.text.util.Linkify
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import androidx.core.net.toUri
import io.noties.markwon.AbstractMarkwonPlugin
import io.noties.markwon.Markwon
import io.noties.markwon.MarkwonConfiguration
import io.noties.markwon.core.CorePlugin
import io.noties.markwon.core.MarkwonTheme
import io.noties.markwon.ext.strikethrough.StrikethroughPlugin
import io.noties.markwon.ext.tables.TableAwareMovementMethod
import io.noties.markwon.ext.tables.TablePlugin
import io.noties.markwon.movement.MovementMethodPlugin

// Adapted from Gotify's released MarkwonFactory. See assets/licenses/gotify-MIT.txt.
internal object MarkwonFactory {
    fun createForMessage(context: Context): Markwon = Markwon.builder(context)
        .usePlugin(CorePlugin.create())
        .usePlugin(MovementMethodPlugin.create(TableAwareMovementMethod.create()))
        .usePlugin(StrikethroughPlugin.create())
        .usePlugin(TablePlugin.create(context))
        .usePlugin(object : AbstractMarkwonPlugin() {
            override fun configureConfiguration(builder: MarkwonConfiguration.Builder) {
                builder.linkResolver { view, link -> openMessageLink(view.context, link) }
            }
            override fun configureTheme(builder: MarkwonTheme.Builder) {
                builder.linkColor(ContextCompat.getColor(context, R.color.hyperLink))
                    .isLinkUnderlined(true)
            }
        })
        .build()
}

internal fun allowedMessageLink(link: String): Boolean = runCatching {
    val uri = link.toUri()
    uri.scheme?.lowercase() in setOf("https", "http") && !uri.host.isNullOrBlank() && uri.userInfo == null
}.getOrDefault(false)

private fun openMessageLink(context: Context, link: String) {
    if (!allowedMessageLink(link)) return
    runCatching { context.startActivity(Intent(Intent.ACTION_VIEW, link.toUri())) }
        .onFailure { Toast.makeText(context, "无法打开链接", Toast.LENGTH_SHORT).show() }
}

internal fun Markwon.renderBody(view: TextView, body: String, contentType: String) {
    view.autoLinkMask = 0
    // Bound parser input before CommonMark/Markwon's recursive visitors touch untrusted alerts.
    val bounded = body.length <= 16_384 && body.count { it in '!'..'/' || it in ':'..'@' || it in '['..'`' || it in '{'..'~' } <= 128
    val markdown = if (contentType == "text/markdown" && bounded) runCatching {
        val root = parse(body)
        val pending = java.util.ArrayDeque<Pair<org.commonmark.node.Node, Int>>()
        pending.add(root to 0)
        var count = 0
        while (pending.isNotEmpty()) {
            val (node, depth) = pending.removeLast()
            check(++count <= 2_000 && depth <= 32)
            var child = node.firstChild
            while (child != null) { pending.add(child to depth + 1); child = child.next }
        }
        render(root)
    }.getOrNull() else null
    if (markdown != null) {
        setParsedMarkdown(view, markdown)
    } else {
        view.text = body
        Linkify.addLinks(view, Linkify.WEB_URLS)
        val spans = view.text as? android.text.Spannable
        spans?.getSpans(0, spans.length, URLSpan::class.java)?.forEach { span ->
            val start = spans.getSpanStart(span)
            val end = spans.getSpanEnd(span)
            spans.removeSpan(span)
            spans.setSpan(object : URLSpan(span.url) {
                override fun onClick(widget: android.view.View) = openMessageLink(widget.context, url)
            }, start, end, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
        }
        view.movementMethod = TableAwareMovementMethod.create()
    }
}
