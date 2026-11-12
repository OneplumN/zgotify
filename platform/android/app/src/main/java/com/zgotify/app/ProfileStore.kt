package com.zgotify.app

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class ProfileStore(context: Context) {
    private val prefs = context.getSharedPreferences("zgotify_personal", Context.MODE_PRIVATE)
    private val alias = "zgotify.profile.tokens.v1"

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
            generateKey()
        }
    }

    private fun encrypt(token: String): String {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key())
        return Base64.encodeToString(cipher.iv + cipher.doFinal(token.toByteArray(Charsets.UTF_8)), Base64.NO_WRAP)
    }

    private fun decrypt(encoded: String): String {
        val data = Base64.decode(encoded, Base64.NO_WRAP)
        require(data.size > 12)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, data.copyOfRange(0, 12)))
        return cipher.doFinal(data.copyOfRange(12, data.size)).toString(Charsets.UTF_8)
    }

    fun all(): List<Profile> {
        val array = JSONArray(prefs.getString("profiles", "[]"))
        return (0 until array.length()).map { index ->
            val item = array.getJSONObject(index)
            val token = try { decrypt(item.getString("token")) } catch (_: Exception) { "" }
            Profile(item.getString("server"), item.getString("username"), token, item.optLong("person_id", 0))
        }
    }

    fun active(): Profile? = all().let { profiles -> profiles.find { it.key == prefs.getString("active", null) } ?: profiles.firstOrNull() }

    fun save(profile: Profile) {
        val profiles = all().filterNot { it.key == profile.key } + profile
        persist(profiles, profile.key)
    }

    fun select(profile: Profile) { prefs.edit().putString("active", profile.key).apply() }

    fun remove(profile: Profile) {
        val remaining = all().filterNot { it.key == profile.key }
        persist(remaining, remaining.firstOrNull()?.key)
    }

    private fun persist(profiles: List<Profile>, active: String?) {
        val array = JSONArray()
        profiles.forEach { array.put(JSONObject().put("server", it.server).put("username", it.username)
            .put("person_id", it.personId).put("token", encrypt(it.token))) }
        check(prefs.edit().putString("profiles", array.toString()).putString("active", active).commit()) { "Could not persist account" }
    }
}
