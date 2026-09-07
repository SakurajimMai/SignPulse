import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import i18n from '../i18n'
import SecretInput from '../components/SecretInput.vue'

describe('SecretInput', () => {
  it('点击小眼睛切换明文并回传 toggle', async () => {
    i18n.global.locale.value = 'zh-CN'
    const wrapper = mount(SecretInput, {
      props: {
        modelValue: '',
        revealed: false,
        placeholder: '已保存',
      },
      global: { plugins: [i18n] },
    })
    expect(wrapper.get('input').element.getAttribute('type')).toBe('password')
    await wrapper.get('button[aria-label="显示"]').trigger('click')
    expect(wrapper.emitted('toggle')).toHaveLength(1)
    await wrapper.setProps({ revealed: true, modelValue: 'secret-value' })
    const input = wrapper.get('input').element as HTMLInputElement
    expect(input.type).toBe('text')
    expect(input.value).toBe('secret-value')
    wrapper.unmount()
  })
})
