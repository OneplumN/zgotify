package com.zgotify.app

import android.content.Context
import android.text.Spanned
import io.noties.markwon.core.spans.StrongEmphasisSpan
import android.widget.FrameLayout
import android.widget.TextView
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class MessagePresentationTest {
    private val context: Context = RuntimeEnvironment.getApplication().apply { setTheme(R.style.AppTheme) }

    @Test fun `plain messages preserve markdown punctuation and unknown formats stay plain`() {
        val view = TextView(context)
        val renderer = MarkwonFactory.createForMessage(context)
        renderer.renderBody(view, "**literal**", "text/plain")
        assertEquals("**literal**", view.text.toString())
        renderer.renderBody(view, "**literal**", "text/html")
        assertEquals("**literal**", view.text.toString())
    }

    @Test fun `explicit markdown renders bold but title remains literal`() {
        val view = TextView(context)
        MarkwonFactory.createForMessage(context).renderBody(view, "**CPU high**", "text/markdown")
        assertEquals("CPU high", view.text.toString())
        val text = view.text as Spanned
        val spans = text.getSpans(0, view.length(), StrongEmphasisSpan::class.java)
        assertEquals(1, spans.size)
        assertEquals(0, text.getSpanStart(spans[0]))
        assertEquals(view.length(), text.getSpanEnd(spans[0]))
    }

    @Test fun `reused row binds current personal state and action after recycling`() {
        val opened = mutableListOf<Long>()
        val actions = mutableListOf<Long>()
        val adapter = ListMessageAdapter(context, { opened.add(it.message.id) }, { actions.add(it.message.id) })
        val holder = adapter.onCreateViewHolder(FrameLayout(context), 0)
        val first = row(42, PersonalState(read = true, hidden = true, favorite = true))
        holder.bind(first)
        assertTrue(holder.state.text.contains("已隐藏"))
        assertEquals("**Title 42**", holder.title.text.toString())
        val next = row(43)
        holder.bind(next)
        assertFalse(holder.state.text.contains("已隐藏"))
        assertFalse(holder.state.text.contains("已收藏"))
        assertTrue(holder.state.text.contains("未读"))
        holder.actions.performClick()
        holder.title.performClick()
        assertEquals(listOf(43L), actions)
        assertEquals(listOf(43L), opened)
        assertEquals("消息操作", holder.actions.contentDescription)
    }

    @Test fun `invalid date is nonfatal and queued changes remain visible`() {
        val adapter = ListMessageAdapter(context, {}, {})
        val holder = adapter.onCreateViewHolder(FrameLayout(context), 0)
        holder.bind(row(1).copy(pending = PendingState("person", 1, 0, read = true)))
        assertTrue(holder.state.text.contains("待同步"))
        assertEquals("", holder.date.text.toString())
    }

    @Test fun `message links allow only HTTP browsing without embedded credentials`() {
        assertTrue(allowedMessageLink("https://monitor.example.com/alert/42"))
        assertTrue(allowedMessageLink("http://monitor.example.com/alert/42"))
        listOf("javascript:alert(1)", "intent://x", "file:///data/data/private", "content://private", "https://user:secret@example.com/", "https:///missing-host")
            .forEach { assertFalse(it, allowedMessageLink(it)) }
    }

    @Test fun `pathologically nested markdown falls back to literal text without parser recursion`() {
        val view = TextView(context)
        val body = "> ".repeat(30_000) + "x"
        MarkwonFactory.createForMessage(context).renderBody(view, body, "text/markdown")
        assertEquals(body, view.text.toString())
    }

    @Test fun `short punctuation depth and node count limits each preserve literal text`() {
        val renderer = MarkwonFactory.createForMessage(context)
        for (body in listOf(
            "> ".repeat(200) + "x",
            "> ".repeat(40) + "x",
            "x\n\n".repeat(1_001),
        )) {
            val view = TextView(context)
            renderer.renderBody(view, body, "text/markdown")
            assertEquals(body, view.text.toString())
        }
    }

    @Test fun `busy refresh cannot reset an older page cursor`() {
        val controller = org.robolectric.Robolectric.buildActivity(MainActivity::class.java).setup()
        try {
            val activity = controller.get()
            val page = MainActivity::class.java.getDeclaredField("page").apply { isAccessible = true }
            val loading = MainActivity::class.java.getDeclaredField("loading").apply { isAccessible = true }
            page.setLong(activity, 42)
            loading.setBoolean(activity, true)
            activity.refreshLatest()
            assertEquals(42, page.getLong(activity))
        } finally { controller.pause().stop().destroy() }
    }

    private fun row(id: Long, state: PersonalState = PersonalState()) = InboxRow(
        InboxMessage(id, 7, "**Title $id**", "body", 9, "invalid", state), null,
    )
}
