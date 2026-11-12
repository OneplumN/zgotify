import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.google.devtools.ksp")
}

dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("androidx.room:room-runtime:2.8.5")
    ksp("androidx.room:room-compiler:2.8.5")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.robolectric:robolectric:4.16")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
}

ksp { arg("room.schemaLocation", "$projectDir/schemas") }

android {
    namespace = "com.zgotify.app"
    compileSdk = 36
    defaultConfig {
        applicationId = "com.zgotify.app"
        minSdk = 26
        targetSdk = 36
        versionCode = 2
        versionName = "0.2.0-internal"
    }
    buildTypes {
        debug { applicationIdSuffix = ".internal" }
        release { isMinifyEnabled = false }
    }
    buildFeatures { buildConfig = true }
    testOptions { unitTests.isIncludeAndroidResources = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlin { compilerOptions { jvmTarget.set(JvmTarget.JVM_17) } }
}
