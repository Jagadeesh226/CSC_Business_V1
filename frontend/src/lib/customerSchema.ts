import { z } from 'zod'

const phoneDigits = (v: string) => v.replace(/\D/g, '')

export const customerSchema = z.object({
  name: z.string().trim().min(1, 'Enter the customer name.').max(200, 'Use 200 characters or fewer.'),
  phone: z
    .string()
    .trim()
    .max(30, 'Use 30 characters or fewer.')
    .refine((v) => v === '' || (phoneDigits(v).length >= 10 && phoneDigits(v).length <= 13), 'Enter a valid mobile number, for example 98765 43210.'),
  email: z.string().trim().refine((v) => v === '' || z.email().safeParse(v).success, 'Enter a valid email address.'),
  address: z.string().trim().max(5000, 'Use 5000 characters or fewer.'),
  notes: z.string().trim().max(5000, 'Use 5000 characters or fewer.'),
})
export type CustomerFormValues = z.infer<typeof customerSchema>
