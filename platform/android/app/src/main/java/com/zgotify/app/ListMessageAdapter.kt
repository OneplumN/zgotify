package com.zgotify.app

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.text.format.DateUtils
import android.view.LayoutInflater
import android.view.ViewGroup
import android.widget.Toast
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import com.zgotify.app.databinding.MessageItemBinding
import java.text.DateFormat
import java.time.OffsetDateTime
import java.util.Date

// Adapted from released Gotify ListMessageAdapter; callbacks never delete shared messages.
internal class ListMessageAdapter(
    private val context: Context,
    private val open: (InboxRow) -> Unit,
    private val action: (InboxRow) -> Unit,
) : ListAdapter<InboxRow, ListMessageAdapter.ViewHolder>(DiffCallback) {
    private val markwon = MarkwonFactory.createForMessage(context)

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int) =
        ViewHolder(MessageItemBinding.inflate(LayoutInflater.from(parent.context), parent, false))

    override fun onBindViewHolder(holder: ViewHolder, position: Int) = holder.bind(getItem(position))

    override fun onViewAttachedToWindow(holder: ViewHolder) {
        super.onViewAttachedToWindow(holder)
        holder.body.isEnabled = false
        holder.body.isEnabled = true
    }

    inner class ViewHolder(private val binding: MessageItemBinding) : RecyclerView.ViewHolder(binding.root) {
        val title = binding.messageTitle
        val body = binding.messageText
        val state = binding.messageState
        val date = binding.messageDate
        val actions = binding.messageActions
        private var relative = true
        private var instant: OffsetDateTime? = null

        fun bind(row: InboxRow) {
            val message = row.message
            title.text = message.title
            title.contentDescription = "${context.getString(R.string.open_message)}：${message.title}"
            markwon.renderBody(body, message.body, message.contentType)
            // Always reset recycled state, including the fallback image and date mode.
            binding.messageImage.setImageResource(R.drawable.gotify)
            relative = true
            instant = runCatching { OffsetDateTime.parse(message.createdAt) }.getOrNull()
            updateDate()
            val flags = listOfNotNull(
                if (message.state.read) "已读" else "未读",
                if (message.state.hidden) "已隐藏" else null,
                if (message.state.favorite) "已收藏" else null,
                if (row.pending?.conflict == true || row.pending?.problem != null) "同步待处理"
                else if (row.pending != null) "待同步" else null,
                "P${message.priority}",
            )
            state.text = flags.joinToString(" · ")
            title.setOnClickListener { open(row) }
            binding.root.setOnClickListener { open(row) }
            actions.setOnClickListener { action(row) }
            date.setOnClickListener { relative = !relative; updateDate() }
            binding.root.setOnLongClickListener {
                context.getSystemService(ClipboardManager::class.java)
                    .setPrimaryClip(ClipData.newPlainText("zgotify", message.body))
                Toast.makeText(context, R.string.message_copied, Toast.LENGTH_SHORT).show()
                true
            }
        }

        private fun updateDate() {
            date.text = instant?.let {
                val time = it.toInstant().toEpochMilli()
                if (relative) DateUtils.getRelativeTimeSpanString(time, System.currentTimeMillis(), DateUtils.MINUTE_IN_MILLIS)
                else DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT).format(Date(time))
            } ?: ""
        }
    }

    private object DiffCallback : DiffUtil.ItemCallback<InboxRow>() {
        override fun areItemsTheSame(oldItem: InboxRow, newItem: InboxRow) = oldItem.message.id == newItem.message.id
        override fun areContentsTheSame(oldItem: InboxRow, newItem: InboxRow) = oldItem == newItem
    }
}
